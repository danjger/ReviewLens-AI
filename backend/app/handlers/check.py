"""Check queue handler (dataset-ingestion task 4.2).

The consumer runtime (:mod:`app.consumer`) routes the ``check`` queue to this
module and reads ``CHECK_QUEUE_URL``; this module supplies the handler
*business logic*. For each ``{check_id, item_id}`` message it:

1. **Claims** the item with a conditional DynamoDB update
   (:func:`app.ingestion.check_session.claim_item`, ``pending``/``error`` →
   ``checking``). If the claim fails (another instance holds it, or it already
   finished) the message is a duplicate/concurrent delivery and is **dropped**
   — the handler returns without error so SQS deletes it (design: "the second
   instance drops it").
2. Runs the viability pipeline, bounded by ``CHECK_TIMEOUT_S`` (default 60):
   **probe** → **capture** to ``checks/{check_id}/{item_id}/`` → read the HTML
   back → **assess** (viability) → **duplicate lookup** → write the verdict,
   final URL, hops, and existing-dataset match onto the item, store the
   Extraction Plan, and move the item to ``done``.
3. Publishes a ``check.updated`` event so the UI learns the result in real time.

Timeout and failure (design "Error Handling"):

- When the whole item takes longer than ``CHECK_TIMEOUT_S``, the item gets a
  ``wont_work`` verdict with reason "Page took too long to load" and the UI
  offers Retry (Requirement 3.10).
- An SSRF block, a non-200 probe, or a non-200 browser main status each yield a
  ``wont_work`` verdict with a plain-language reason (Requirements 2.2, 2.7).
- An unexpected crash lets SQS retry; on the **final** attempt the item is set
  to ``error`` so the UI can offer Retry rather than leaving it stuck
  ``checking``.

Refresh-origin sessions (``origin = "refresh"``, Requirement 6.9) are handled by
:func:`_handle_refresh_origin` (dataset-ingestion task 6.2). The Library's
Refresh action starts a one-item ``refresh`` session carrying the target
dataset id, then this handler runs the *same* probe → capture → assess pipeline
against the dataset's original URL and, from the fresh verdict:

- **auto-refreshes** (via :mod:`app.datasets.refresh_service`) when the verdict
  is ``will_work``, or ``limited`` for a dataset whose previous verdict was also
  ``limited`` — no analyst interaction needed;
- leaves the item ``awaiting_confirmation`` when the verdict is ``limited`` for a
  dataset previously ``will_work`` (the analyst confirms);
- **creates no new data version** on ``wont_work``: the existing data and status
  are kept and a failed-refresh event is appended through
  :func:`app.db.status.log_event`.

Engineering rules honoured: stateless (no process memory drives correctness);
same code in both compute modes (no Lambda event shapes imported here);
idempotent (the conditional claim makes duplicate/concurrent delivery safe);
SSRF-guarded fetches via the probe and the capture route guard; every S3 key
from :mod:`app.storage.keys`; the AI call happens inside ``assess`` through the
instrumented client; no raw client IP is stored or logged.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from app.capture import engine as capture_engine
from app.consumer import MessageMeta
from app.core.config import get_settings
from app.core.db import session_scope
from app.datasets import refresh_service
from app.datasets.refresh_service import CheckCapture
from app.db.models import Dataset
from app.db.status import log_event
from app.events.publisher import publish_event
from app.ingestion import check_session, duplicates
from app.ingestion.check_session import CheckItem, CheckSession
from app.ingestion.robots import check as robots_check
from app.ingestion.url_normalizer import normalize
from app.ingestion.url_validator import SsrfError, probe
from app.ingestion.verdict import Verdict
from app.ingestion.viability import CaptureView, assess
from app.storage import keys, s3

logger = logging.getLogger(__name__)

#: EventBridge detail-type published when a check item's result changes.
CHECK_UPDATED_DETAIL_TYPE = "check.updated"

#: Reason text for the per-URL timeout (Requirement 3.10, design Error table).
_TIMEOUT_REASON = "Page took too long to load"


@dataclass
class _CheckOutcome:
    """What the pipeline produced for one item, before it is written back.

    ``verdict`` is always present; ``plan`` is the Extraction Plan JSON to store
    at ``checks/{check_id}/{item_id}/plan.json`` (``None`` means no plan to
    store, e.g. a probe/SSRF failure before capture). ``final_url`` and ``hops``
    are recorded on the item; ``existing_dataset`` is the duplicate-lookup match
    (``None`` when the URL is not tracked).
    """

    verdict: Verdict
    plan_json: dict[str, Any] | None
    final_url: str | None
    hops: list[dict[str, Any]]
    existing_dataset: dict[str, Any] | None


class CheckHandler:
    """Queue handler that turns one submitted URL into a verdict and plan."""

    queue: str = "check"

    def handle(self, body: dict[str, Any], meta: MessageMeta) -> None:
        check_id = body["check_id"]
        item_id = body["item_id"]

        # 1. Conditional claim. A lost claim means a duplicate or concurrent
        #    delivery (or an already-finished item): drop it (design: "the
        #    second instance drops it"). Returning normally lets SQS delete it.
        if not check_session.claim_item(check_id, item_id):
            logger.info(
                "check %s/%s already claimed or finished; dropping delivery",
                check_id,
                item_id,
            )
            return

        session = check_session.get_session(check_id)
        if session is None:
            # The session expired (TTL) between claim and read: nothing to do.
            logger.warning("check session %s vanished after claim; dropping", check_id)
            return
        item = session.items.get(item_id)
        if item is None:  # pragma: no cover - claim proved the row exists
            logger.warning("check item %s/%s missing after claim; dropping", check_id, item_id)
            return

        # Refresh-origin sessions are task 6.2's responsibility.
        if session.origin == "refresh":
            self._handle_refresh_origin(session, item, meta)
            return

        try:
            outcome = self._run_with_timeout(check_id, item)
        except Exception:
            # Unexpected crash: let SQS retry. On the final attempt, record the
            # item as `error` first so the UI can offer Retry instead of leaving
            # it stuck in `checking`, then re-raise so the message is retried /
            # moved to the DLQ by the runtime.
            self._mark_error(check_id, item_id, meta)
            raise

        self._write_outcome(check_id, item, outcome)
        self._publish_updated(check_id, item_id, outcome.verdict)

    # ------------------------------------------------------------------
    # Pipeline (bounded by CHECK_TIMEOUT_S)
    # ------------------------------------------------------------------

    def _run_with_timeout(self, check_id: str, item: CheckItem) -> _CheckOutcome:
        """Run the per-item pipeline, bounded by ``CHECK_TIMEOUT_S``.

        The pipeline (probe → capture → assess → duplicate lookup) is blocking
        and synchronous, so it runs in a daemon worker thread that is joined for
        at most the configured budget. On timeout the item gets a ``wont_work``
        "Page took too long to load" verdict (Requirement 3.10). The worker
        thread is left to finish on its own (it is a daemon and holds no shared
        locks beyond its own capture context), so a hung page never blocks the
        consumer past the budget.
        """
        timeout_s = get_settings().check_timeout_s

        result: list[_CheckOutcome] = []
        error: list[BaseException] = []

        def _work() -> None:
            try:
                result.append(self._pipeline(check_id, item))
            except BaseException as exc:  # noqa: BLE001 - carried to the caller thread
                error.append(exc)

        worker = threading.Thread(
            target=_work,
            name=f"check-{check_id[:8]}-{item.item_id}",
            daemon=True,
        )
        worker.start()
        worker.join(timeout=timeout_s)

        if worker.is_alive():
            logger.warning(
                "check %s/%s exceeded CHECK_TIMEOUT_S=%ss; verdict wont_work",
                check_id,
                item.item_id,
                timeout_s,
            )
            return self._timeout_outcome(item)

        if error:
            raise error[0]
        return result[0]

    def _pipeline(self, check_id: str, item: CheckItem) -> _CheckOutcome:
        """Probe, capture, assess, and look up duplicates for one item."""
        target_url = item.input

        # Probe: follow redirects, SSRF-check every hop, require a final 200.
        try:
            probe_result = probe(target_url)
        except SsrfError:
            return _CheckOutcome(
                verdict=_wont_work("Address not allowed"),
                plan_json=None,
                final_url=None,
                hops=[],
                existing_dataset=None,
            )

        hops = [asdict(hop) for hop in probe_result.hops]
        if not probe_result.ok:
            # Non-200 final status, loop, or too many redirects (Req 2.2, 2.3).
            reason = probe_result.reason or f"Status {probe_result.status}"
            return _CheckOutcome(
                verdict=_wont_work(reason),
                plan_json=None,
                final_url=probe_result.final_url,
                hops=hops,
                existing_dataset=None,
            )

        final_url = probe_result.final_url

        # Capture: render under the item's check prefix (every browser request
        # SSRF-checked by the capture route guard).
        prefix = item.capture_prefix or keys.check_prefix(check_id, item.item_id)
        try:
            capture = capture_engine.render(final_url, prefix)
        except capture_engine.CaptureBlockedError:
            return _CheckOutcome(
                verdict=_wont_work("Address not allowed"),
                plan_json=None,
                final_url=final_url,
                hops=hops,
                existing_dataset=None,
            )

        # The browser may redirect itself (meta refresh / script): record a
        # client-side hop and use the page the browser ended on (Req 2.8).
        if capture.redirected:
            hops.append(
                {
                    "url": capture.final_url,
                    "status": capture.main_status,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "kind": "client_redirect",
                }
            )
            final_url = capture.final_url

        # The main document's browser status must also be 200 (Req 2.7).
        if capture.main_status is not None and capture.main_status != 200:
            return _CheckOutcome(
                verdict=_wont_work(f"Status {capture.main_status}"),
                plan_json=None,
                final_url=final_url,
                hops=hops,
                existing_dataset=None,
            )

        # Read the rendered HTML back from S3 for the viability assessment, and
        # evaluate robots.txt for the warning (never changes the verdict).
        html = s3.get_text(capture.html_key)
        robots = robots_check(final_url)
        view = CaptureView.from_capture(capture, html)
        verdict, plan = assess(view, final_url, robots)

        # Duplicate lookup: is this URL already tracked (archived included)?
        existing = duplicates.find_existing(item.normalized, _normalize(final_url))

        return _CheckOutcome(
            verdict=verdict,
            plan_json=plan.model_dump(mode="json"),
            final_url=final_url,
            hops=hops,
            existing_dataset=existing.to_dict() if existing else None,
        )

    def _timeout_outcome(self, item: CheckItem) -> _CheckOutcome:
        """Build the ``wont_work`` outcome for a timed-out item (Req 3.10)."""
        return _CheckOutcome(
            verdict=_wont_work(_TIMEOUT_REASON),
            plan_json=None,
            final_url=item.final_url,
            hops=item.hops,
            existing_dataset=None,
        )

    # ------------------------------------------------------------------
    # Write-back and events
    # ------------------------------------------------------------------

    def _write_outcome(self, check_id: str, item: CheckItem, outcome: _CheckOutcome) -> None:
        """Store the plan, then write the verdict and finish the item.

        The plan is stored under ``checks/{check_id}/{item_id}/plan.json`` (only
        when the pipeline produced one). The item fields are written with a
        conditional ``expected_state="checking"`` guard so only the worker that
        holds the claim writes the result; a stale write (another worker already
        finished it) is dropped. The ``check.updated`` event is published after.
        """
        self._store_plan(check_id, item, outcome)

        fields: dict[str, Any] = {
            "state": "done",
            "verdict": outcome.verdict.to_dict(),
            "hops": outcome.hops,
            "existing_dataset": outcome.existing_dataset,
        }
        if outcome.final_url is not None:
            fields["final_url"] = outcome.final_url

        check_session.set_item_fields(
            check_id,
            item.item_id,
            fields,
            expected_state="checking",
        )

    def _publish_updated(self, check_id: str, item_id: str, verdict: Verdict) -> None:
        """Publish ``check.updated`` so the UI learns the result in real time."""
        publish_event(
            detail_type=CHECK_UPDATED_DETAIL_TYPE,
            detail={
                "check_id": check_id,
                "item_id": item_id,
                "state": "done",
                "verdict": verdict.verdict,
            },
        )

    def _mark_error(self, check_id: str, item_id: str, meta: MessageMeta) -> None:
        """On the final SQS attempt, move the item to ``error`` with Retry.

        Only the final attempt flips the state so earlier retries can still
        re-claim the item from ``checking``/``error`` and try again. The item is
        guarded to the ``checking`` state so a concurrent success is not
        clobbered. A ``check.updated`` event tells the UI to show Retry.
        """
        if not meta.is_final_attempt:
            return
        wrote = check_session.set_item_fields(
            check_id,
            item_id,
            {"state": "error"},
            expected_state="checking",
        )
        if wrote:
            publish_event(
                detail_type=CHECK_UPDATED_DETAIL_TYPE,
                detail={
                    "check_id": check_id,
                    "item_id": item_id,
                    "state": "error",
                },
            )

    # ------------------------------------------------------------------
    # Refresh origin (seam for dataset-ingestion task 6.2)
    # ------------------------------------------------------------------

    def _handle_refresh_origin(
        self,
        session: CheckSession,
        item: CheckItem,
        meta: MessageMeta,
    ) -> None:
        """Handle a ``refresh``-origin item (Requirement 6.9).

        The Library's Refresh action started this one-item session with
        ``origin = "refresh"`` and ``refresh_dataset_id`` set to the dataset
        being refreshed. We run the *same* check pipeline (probe → capture →
        assess) against the dataset's original URL (the item's ``input``), store
        the fresh plan and verdict on the item, then act on the verdict:

        - ``will_work``, or ``limited`` for a dataset whose previous verdict was
          also ``limited``: start the refresh automatically through
          :func:`app.datasets.refresh_service.refresh` with a
          :class:`~app.datasets.refresh_service.CheckCapture` built from this
          check's S3 artifacts, and mark the item ``applied``.
        - ``limited`` for a dataset previously ``will_work``: do **not** refresh;
          leave the item ``awaiting_confirmation`` for the analyst.
        - ``wont_work``: create **no** new data version. Keep the existing data
          and status, and append a failed-refresh event through
          :func:`app.db.status.log_event` carrying the reason. The item is
          finished (``done``) so the Library stops showing a refresh in flight.

        A ``check.updated`` event is published in every branch so the UI learns
        the outcome. Crashes are handled exactly like the ``new`` path: the item
        is moved to ``error`` on the final SQS attempt and the exception
        re-raised so SQS redrives.

        Idempotency: the conditional claim already dropped duplicate deliveries
        before we get here; the Refresh Service's own concurrency guard means a
        racing automatic refresh still yields exactly one new version.
        """
        dataset_id = session.refresh_dataset_id
        check_id = session.check_id
        if dataset_id is None:  # pragma: no cover - a refresh session always carries it
            raise ValueError(f"refresh session {check_id} has no refresh_dataset_id")

        try:
            outcome = self._run_with_timeout(check_id, item)
        except Exception:
            self._mark_error(check_id, item.item_id, meta)
            raise

        # Store the plan (so the CheckCapture's plan_key exists) and record the
        # fresh verdict/final URL/hops on the item, exactly like the new path.
        self._store_plan(check_id, item, outcome)

        label = outcome.verdict.verdict
        if label == "wont_work":
            # No new data version; keep existing data/status, log the failure.
            self._refresh_failed(check_id, dataset_id, item, outcome)
            return

        previous = _previous_verdict(dataset_id)
        if label == "will_work" or (label == "limited" and previous == "limited"):
            self._refresh_automatically(check_id, dataset_id, item, outcome)
        else:
            # limited after a previous will_work: wait for the analyst.
            self._finish_refresh_item(check_id, item, outcome, state="awaiting_confirmation")

    def _refresh_automatically(
        self,
        check_id: str,
        dataset_id: str,
        item: CheckItem,
        outcome: _CheckOutcome,
    ) -> None:
        """Start the refresh and mark the item ``applied`` (Requirement 6.9).

        The refresh re-uses this Check's capture: the rendered page, plan, and
        (when present) snapshot are copied into the dataset's new ``raw/v{n}``
        by the Refresh Service. The item is then finished ``applied`` so the UI
        shows the refresh was started without the analyst staying on the page.
        """
        capture = _check_capture(check_id, item)
        refresh_service.refresh(dataset_id, trigger="manual_refresh", capture=capture)
        self._finish_refresh_item(check_id, item, outcome, state="applied")

    def _refresh_failed(
        self,
        check_id: str,
        dataset_id: str,
        item: CheckItem,
        outcome: _CheckOutcome,
    ) -> None:
        """Record a failed refresh with no new version (Requirement 6.9).

        A refresh whose Check ends ``wont_work`` never creates a
        ``dataset_versions`` row: we only append a failed-refresh event through
        ``db.status.log_event`` (no status transition, so the dataset keeps its
        existing status and data), then finish the item ``done``.
        """
        reasons = outcome.verdict.reasons
        reason = reasons[0] if reasons else "The page can't be read right now"
        log_event(
            dataset_id,
            f"Refresh failed: {reason}",
            extra={"trigger": "manual_refresh", "verdict": "wont_work", "reasons": reasons},
        )
        self._finish_refresh_item(check_id, item, outcome, state="done")

    def _finish_refresh_item(
        self,
        check_id: str,
        item: CheckItem,
        outcome: _CheckOutcome,
        *,
        state: str,
    ) -> None:
        """Write the verdict/final URL/hops and the terminal ``state`` on the item.

        Guarded by ``expected_state="checking"`` so only the worker that holds
        the claim writes the result, then publishes ``check.updated`` carrying
        the item's new state and the verdict label.
        """
        fields: dict[str, Any] = {
            "state": state,
            "verdict": outcome.verdict.to_dict(),
            "hops": outcome.hops,
            "existing_dataset": outcome.existing_dataset,
        }
        if outcome.final_url is not None:
            fields["final_url"] = outcome.final_url

        check_session.set_item_fields(
            check_id,
            item.item_id,
            fields,
            expected_state="checking",
        )
        publish_event(
            detail_type=CHECK_UPDATED_DETAIL_TYPE,
            detail={
                "check_id": check_id,
                "item_id": item.item_id,
                "state": state,
                "verdict": outcome.verdict.verdict,
            },
        )

    def _store_plan(self, check_id: str, item: CheckItem, outcome: _CheckOutcome) -> None:
        """Store the Extraction Plan under the check prefix (when one exists)."""
        if outcome.plan_json is not None:
            s3.put_bytes(
                keys.check_plan(check_id, item.item_id),
                json.dumps(outcome.plan_json).encode("utf-8"),
                content_type="application/json",
            )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _wont_work(reason: str) -> Verdict:
    """Build a bare ``wont_work`` verdict for a pre-capture failure.

    Used for an SSRF block, a non-200 probe/browser status, a redirect problem,
    or a timeout — cases where there is no Extraction Plan or evidence to show,
    just the reason the URL cannot be added (design Error Handling table).
    """
    return Verdict(verdict="wont_work", reasons=[reason])


def _normalize(url: str | None) -> str | None:
    """Normalize a final URL for the duplicate lookup (``None`` passes through)."""
    if url is None:
        return None
    return normalize(url)


def _previous_verdict(dataset_id: str) -> str | None:
    """Return a dataset's last recorded viability verdict label, or ``None``.

    The previous verdict is stored under ``status_detail.viability`` when the
    dataset was added or last refreshed (dataset-ingestion task 5.2 / 7.1). The
    refresh-origin rules (Requirement 6.9) use it to tell a ``limited`` result
    for a previously ``limited`` dataset (auto-refresh) from one for a
    previously ``will_work`` dataset (wait for confirmation).

    Reads the database only through ``core.db``. A missing dataset or absent
    viability block yields ``None`` (treated as "no previous will_work", so a
    fresh ``limited`` waits for confirmation — the safe default).
    """
    with session_scope() as session:
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:
            return None
        viability = (dataset.status_detail or {}).get("viability")
    if not isinstance(viability, dict):
        return None
    verdict = viability.get("verdict")
    return verdict if isinstance(verdict, str) else None


def _check_capture(check_id: str, item: CheckItem) -> CheckCapture:
    """Build the :class:`CheckCapture` for a completed refresh-origin item.

    Every S3 key comes from :mod:`app.storage.keys`: the rendered page and the
    Extraction Plan are always named; the above-the-fold snapshot is offered
    too (the Refresh Service copies it only when the object exists).
    """
    return CheckCapture(
        page_key=keys.check_page(check_id, item.item_id),
        plan_key=keys.check_plan(check_id, item.item_id),
        snapshot_key=keys.check_snapshot(check_id, item.item_id),
    )


handler = CheckHandler()
