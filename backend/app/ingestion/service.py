"""Add service: turn chosen Check items into datasets or refreshes.

This module implements the **Add** step of URL ingestion (dataset-ingestion
task 7.1). The analyst has run a Check (`POST /ingest/checks`), each submitted
URL now carries a verdict on its Check Session item, and the analyst picks the
ones to add. :func:`add_items` processes that choice item by item and returns a
per-item outcome from the design's vocabulary:

    created | refreshed | restored_and_refreshed | already_refreshing
    | refused_wont_work | needs_confirmation | expired

For each requested item ``{item_id, confirm_limited?}``:

1. **Expired session** (design Error Handling): if the Check Session is gone
   (TTL), every requested item's outcome is ``expired`` — the UI asks the
   analyst to check again (Requirement 5.4).
2. **``wont_work`` → refuse** (Requirement 3.8, Correctness Property 5): no
   dataset and no data version is ever created from a ``wont_work`` item.
   Outcome ``refused_wont_work``.
3. **``limited`` without ``confirm_limited``** (Requirement 3.9): the analyst
   must confirm a limited URL; without confirmation nothing is created. Outcome
   ``needs_confirmation``.
4. **Conditional ``applied`` marking** (design "Backend modules", Error
   Handling): before doing any work the item is flipped ``done`` →``applied``
   with a conditional DynamoDB update (:func:`check_session.set_item_fields`
   with ``expected_state="done"``). The loser of a double-clicked Add — or a
   second worker — fails the condition and does **not** create a second
   dataset. An item already ``applied`` is treated as idempotent success.
5. **Already tracked** (Requirement 6.4–6.6): if the Check found an existing
   dataset for this URL, Add does **not** create a copy. It routes to the
   shared :func:`app.datasets.refresh_service.refresh` with
   ``trigger="duplicate_submission"`` and the Check's capture, mapping the
   Refresh Service's result to ``refreshed`` / ``restored_and_refreshed`` /
   ``already_refreshing``. The ``refresh_requested``/``duplicate_submission``
   event is appended inside the Refresh Service, so it is not duplicated here.
6. **New dataset** (Requirement 5.1–5.3): otherwise :func:`create_from_check`
   copies the capture (page, plan, and snapshot) from the Check prefix into the
   dataset's permanent location, inserts the ``datasets`` row (status
   ``requested``) with the viability verdict in ``status_detail`` and a
   ``requested`` event carrying the redirect details and viability result,
   inserts a ``dataset_versions`` v1 row with trigger ``initial``, and enqueues
   a processing message onto the FIFO queue. Outcome ``created`` with the new
   ``dataset_id``.

Failure cleanup (Requirement 4.5): if copying objects or inserting the record
fails, :func:`create_from_check` deletes any partial permanent objects and does
**not** leave a half-created dataset behind.

Engineering rules honoured:

- **Stateless.** No correctness depends on process memory; shared state lives in
  Aurora, S3, DynamoDB, and SQS.
- **Idempotent / concurrency-safe.** The conditional ``applied`` marking stops a
  double-submitted Add from creating two datasets; the Refresh Service's own
  guard stops a tracked URL from starting two refreshes.
- **DB only through ``core.db``**; the ``requested`` status/event is written in
  the same INSERT so the new row is born with its event log (every later status
  change still goes through ``db.status``); every S3 key comes from
  :mod:`app.storage.keys`; the queue body carries IDs only.

The straightforward INSERT path and the unique-constraint race fallback are
both implemented here (dataset-ingestion tasks 7.1 and 7.2). When two concurrent
Adds of the same *new* normalized URL race, the partial unique index
``uq_datasets_normalized_url`` lets one INSERT win; the loser's INSERT raises an
``IntegrityError`` on that index, and :func:`create_from_check` catches that
specific violation, cleans up its partial permanent objects, looks the winner up
by normalized URL, and falls back to refreshing it — so exactly one dataset
exists for the URL afterward (Requirement 6.7, Correctness Property 6). The
``POST .../add`` endpoint is task 7.3.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.queue import enqueue
from app.datasets import refresh_service
from app.datasets.refresh_service import CheckCapture
from app.db.models import DatasetStatus, SourceType
from app.ingestion import check_session, duplicates, upload_parser
from app.ingestion.check_session import CheckItem, CheckSession
from app.ingestion.upload_parser import UploadPreview
from app.storage import keys, s3

#: Name of the unique partial index that enforces "one dataset per normalized
#: URL" for URL datasets (``app.db.models`` / migration 0001). A concurrent Add
#: that lost the race violates this index; its name is how we tell that specific
#: unique violation apart from any other ``IntegrityError`` (Requirement 6.7).
_UNIQUE_URL_INDEX = "uq_datasets_normalized_url"

logger = logging.getLogger(__name__)

#: The per-item Add outcomes (design "API endpoints": ``outcome``).
Outcome = Literal[
    "created",
    "refreshed",
    "restored_and_refreshed",
    "already_refreshing",
    "refused_wont_work",
    "needs_confirmation",
    "expired",
]

#: Trigger stored on the first ``dataset_versions`` row of a new dataset.
_INITIAL_TRIGGER = "initial"


# ---------------------------------------------------------------------------
# Request / result value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AddRequestItem:
    """One item the analyst chose to add.

    Attributes:
        item_id: The Check Session item to add.
        confirm_limited: True when the analyst confirmed adding a ``limited``
            URL (Requirement 3.9). Ignored for other verdicts.
        name: The analyst-supplied dataset name. Required for an HTML-upload
            item (Requirement 8.8) and ignored for URL items (whose name is
            derived from the page title / URL). Carried from the Add body and
            used only on the HTML-upload create path.
        source_url: The analyst's optional original page URL for an HTML upload
            (Requirement 8.8). Stored for display/provenance and used for
            duplicate matching only; never fetched. Ignored for URL items.
        description: Optional analyst-supplied source description for an HTML
            upload. Ignored for URL items.
    """

    item_id: str
    confirm_limited: bool = False
    name: str | None = None
    source_url: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class AddResult:
    """The outcome of adding one item (one row of the endpoint's ``results``)."""

    item_id: str
    outcome: Outcome
    dataset_id: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "outcome": self.outcome,
            "dataset_id": self.dataset_id,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def add_items(check_id: str, items: list[AddRequestItem]) -> list[AddResult]:
    """Add the chosen Check items, returning a per-item outcome.

    See the module docstring for the full per-item decision table. The Check
    Session is read once; if it has expired every requested item's outcome is
    ``expired`` (Requirement 5.4, design Error Handling).

    Args:
        check_id: The Check Session the items belong to.
        items: The items the analyst chose, each with an optional
            ``confirm_limited`` flag.

    Returns:
        One :class:`AddResult` per requested item, in the same order.
    """
    session = check_session.get_session(check_id)
    if session is None:
        # TTL expired (or never existed): nothing to add (design Error table).
        logger.info("add_items: check session %s expired/absent", check_id)
        return [AddResult(it.item_id, "expired") for it in items]

    return [_add_one(session, it) for it in items]


def _add_one(session: CheckSession, request: AddRequestItem) -> AddResult:
    """Process one requested item against its Check Session."""
    check_id = session.check_id
    item = session.items.get(request.item_id)
    if item is None:
        # An item id that was never part of this session: treat like expired so
        # the UI prompts a re-check rather than silently succeeding.
        logger.info("add_items: item %s not in session %s", request.item_id, check_id)
        return AddResult(request.item_id, "expired")

    # An already-applied item is idempotent success: a retried Add must not
    # create or refresh a second time. We cannot know the original outcome from
    # the item alone, so report the generic in-flight state.
    if item.state == "applied":
        logger.info("add_items: item %s/%s already applied", check_id, item.item_id)
        return AddResult(item.item_id, _reapplied_outcome(item))

    label = _verdict_label(item)

    # wont_work: never becomes data (Requirement 3.8, Property 5).
    if label == "wont_work":
        return AddResult(
            item.item_id,
            "refused_wont_work",
            message="This page can't be read, so it can't be added.",
        )

    # limited needs explicit confirmation (Requirement 3.9).
    if label == "limited" and not request.confirm_limited:
        return AddResult(
            item.item_id,
            "needs_confirmation",
            message="This URL is limited. Confirm to add it.",
        )

    # Conditional applied marking (done → applied). The loser of a concurrent /
    # double-clicked Add fails the condition and must not create a dataset.
    claimed = check_session.set_item_fields(
        check_id,
        item.item_id,
        {"state": "applied"},
        expected_state="done",
    )
    if not claimed:
        # Another Add already claimed this item. Report in-flight rather than
        # creating a duplicate (design Error Handling: the loser reports the
        # winner's effect; without the winner's result we use the safe generic).
        logger.info("add_items: item %s/%s already claimed by another Add", check_id, item.item_id)
        return AddResult(item.item_id, _reapplied_outcome(item))

    # An HTML-upload item carries ``upload_id`` instead of a URL. Its per-item
    # decision table is identical up to here (refuse wont_work, confirm limited,
    # the applied claim); only the create-vs-refresh branch differs — the create
    # path is create_from_html_upload, and routing to refresh happens only when
    # the optional source URL matched an existing dataset at Check time. An
    # upload with no source URL ALWAYS creates (Requirement 8.11); one whose
    # source URL is tracked refreshes via the shared Refresh Service with
    # trigger ``upload_replace`` (Requirement 8.12).
    if item.upload_id is not None:
        if item.existing_dataset:
            return _route_html_upload_to_refresh(check_id, item)
        return create_from_html_upload(
            check_id,
            item,
            name=request.name,
            source_url=request.source_url,
            description=request.description,
        )

    # Already tracked → refresh the existing dataset (Requirement 6.4).
    if item.existing_dataset:
        return _route_to_refresh(check_id, item)

    # New URL → create a brand new dataset (Requirement 5.1–5.3). If a
    # concurrent Add created the same normalized URL first, this falls back to
    # refreshing the winner's dataset (Requirement 6.7, Property 6).
    return create_from_check(check_id, item)


# ---------------------------------------------------------------------------
# Tracked URL → Refresh Service (Requirement 6.4–6.6)
# ---------------------------------------------------------------------------


def _route_to_refresh(check_id: str, item: CheckItem) -> AddResult:
    """Refresh the already-tracked dataset using this Check's capture.

    The ``refresh_requested`` event with trigger ``duplicate_submission`` is
    appended **inside** :func:`app.datasets.refresh_service.refresh`, so it is
    not duplicated here. The Refresh Service's own guard makes a concurrent
    refresh safe (``already_refreshing``).
    """
    assert item.existing_dataset is not None  # guarded by the caller
    existing_id = str(item.existing_dataset["id"])
    capture = _check_capture(check_id, item)

    outcome = refresh_service.refresh(
        existing_id,
        trigger="duplicate_submission",
        capture=capture,
    )
    # refresh_service's RefreshOutcome literals are a subset of our Outcome.
    return AddResult(item.item_id, outcome, dataset_id=existing_id)


def _route_html_upload_to_refresh(check_id: str, item: CheckItem) -> AddResult:
    """Refresh the dataset an HTML upload's source URL already tracks (Req 8.12).

    When the analyst supplied a ``source_url`` whose normalized form matched an
    existing dataset at Check time, the upload refreshes that dataset rather
    than creating a competing ``html_upload`` row for the same URL. The capture
    is a :class:`CheckCapture` carrying the uploaded HTML (copied by the Check
    handler to ``checks/{check_id}/{item_id}/page.html``) and its Extraction
    Plan (``plan.json``) with **no snapshot** — exactly the shape a URL Check
    produces, so the Refresh Service's URL branch copies it unchanged. The
    trigger is ``upload_replace`` (design: an HTML-upload refresh uses
    ``upload_replace``). The ``refresh_requested`` event is appended inside the
    Refresh Service, so it is not duplicated here.
    """
    assert item.existing_dataset is not None  # guarded by the caller
    existing_id = str(item.existing_dataset["id"])
    # The uploaded HTML + plan live under the same check keys a URL capture
    # uses (the Check handler wrote them there); no snapshot is produced for an
    # upload, so _check_capture offers a snapshot key the Refresh Service simply
    # finds absent and skips.
    capture = _check_capture(check_id, item)

    outcome = refresh_service.refresh(
        existing_id,
        trigger="upload_replace",
        capture=capture,
    )
    return AddResult(item.item_id, outcome, dataset_id=existing_id)


# ---------------------------------------------------------------------------
# New URL → create a dataset from the Check capture (Requirement 5.1–5.3)
# ---------------------------------------------------------------------------


def create_from_check(check_id: str, item: CheckItem) -> AddResult:
    """Create a new dataset (v1) from a completed Check item.

    Copies the Check's capture (page → ``raw/v1/page-1.html``, plan →
    ``raw/v1/plan.json``, snapshot → ``snapshot/v1.png`` when present) into the
    dataset's permanent location, then inserts the ``datasets`` row and its
    ``dataset_versions`` v1 row in one transaction, and finally enqueues a
    processing message. The row is born ``requested`` with ``status_detail``
    holding the viability verdict (Requirement 3.11) and a ``requested`` event
    carrying the redirect details and viability result (Requirement 5.2); the
    redirect hops are also stored under ``status_detail.redirects`` (Requirement
    2.5).

    On any failure while copying objects or inserting the record, every partial
    permanent object is deleted and no dataset row is left behind (Requirement
    4.5).

    **Unique-constraint race (task 7.2, Requirement 6.7, Correctness Property
    6).** The duplicate lookup at Check time can miss a *concurrent* Add of the
    same new URL: both Adds see "not tracked" and both try to create a dataset.
    The partial unique index ``uq_datasets_normalized_url`` lets exactly one
    INSERT win; the loser's INSERT raises :class:`~sqlalchemy.exc.IntegrityError`
    on that index. When that specific violation is seen this function treats the
    Add like an already-tracked URL: it cleans up its own partial permanent
    objects (Requirement 4.5 still holds for the losing dataset id), looks up
    the winner by normalized URL, and falls back to
    :func:`app.datasets.refresh_service.refresh` with trigger
    ``duplicate_submission`` — so afterward exactly one dataset exists for that
    URL (Property 6). Any *other* ``IntegrityError`` is not swallowed: the
    partial objects are cleaned up and the error propagates.

    Args:
        check_id: The Check Session the item belongs to.
        item: The completed, viable, not-already-tracked Check item.

    Returns:
        An :class:`AddResult`: ``created`` with the new dataset's id on the
        straightforward path, or the Refresh Service's outcome
        (``refreshed`` / ``restored_and_refreshed`` / ``already_refreshing``)
        with the winner's id when the unique-constraint race fired.
    """
    dataset_id = _new_dataset_id()

    # Destination keys (every key from storage.keys). v1 is the first version.
    page_dst = keys.dataset_raw_page(dataset_id, 1, 1)
    plan_dst = keys.dataset_raw_plan(dataset_id, 1)
    snapshot_dst = keys.dataset_snapshot(dataset_id, 1)

    src_page = keys.check_page(check_id, item.item_id)
    src_plan = keys.check_plan(check_id, item.item_id)
    src_snapshot = keys.check_snapshot(check_id, item.item_id)

    copied: list[str] = []
    try:
        s3.copy_object(src_page, page_dst)
        copied.append(page_dst)
        s3.copy_object(src_plan, plan_dst)
        copied.append(plan_dst)
        if s3.object_exists(src_snapshot):
            s3.copy_object(src_snapshot, snapshot_dst)
            copied.append(snapshot_dst)

        _insert_dataset(dataset_id, item)
    except IntegrityError as exc:
        # A concurrent Add may have inserted the same normalized URL first. If
        # this is that specific unique violation, fall back to refreshing the
        # winner (Requirement 6.7, Property 6); otherwise re-raise. Either way
        # the losing dataset's partial permanent objects are removed first so no
        # orphaned objects remain under its id (Requirement 4.5).
        _cleanup_partial(copied, check_id, item.item_id)
        if not _is_duplicate_url_violation(exc):
            raise
        return _fallback_to_winner(check_id, item)
    except Exception:
        # Requirement 4.5: delete any partial permanent objects and do not
        # create (or leave) the record. The INSERT is one transaction, so a
        # failed INSERT rolled itself back; only the copied S3 objects remain,
        # and we remove them here.
        logger.exception(
            "create_from_check failed for check %s item %s; cleaning up %d object(s)",
            check_id,
            item.item_id,
            len(copied),
        )
        _cleanup_partial(copied, check_id, item.item_id)
        raise

    # Hand the new version to review-analysis. Body carries IDs only; the
    # processing queue is FIFO keyed by dataset_id (one group per dataset).
    enqueue(
        get_settings().processing_queue_url,
        {"dataset_id": dataset_id, "data_version": 1},
        message_group_id=dataset_id,
        # The processing FIFO queue has content-based dedup OFF (api-stack.ts),
        # so the producer MUST supply the dedup id. Keyed dataset_id:version so a
        # re-enqueue of the same version is de-duplicated (design "FIFO dedup").
        message_deduplication_id=f"{dataset_id}:1",
    )

    return AddResult(item.item_id, "created", dataset_id=dataset_id)


# ---------------------------------------------------------------------------
# HTML upload → create a dataset from the uploaded page (Requirement 8.8, 8.9)
# ---------------------------------------------------------------------------


def create_from_html_upload(
    check_id: str,
    item: CheckItem,
    *,
    name: str | None,
    source_url: str | None,
    description: str | None,
) -> AddResult:
    """Create a new ``html_upload`` dataset (v1) from an assessed HTML upload.

    The HTML-upload analogue of :func:`create_from_check` (design
    ``ingestion.service.create_from_html_upload`` bullet, Requirements 8.8, 8.9).
    The Check handler has already assessed the uploaded page and copied the
    uploaded HTML to ``checks/{check_id}/{item_id}/page.html`` and the Extraction
    Plan to ``plan.json`` under the same check prefix a URL capture uses, so this
    reuses the same copy-into-``raw/v1`` + insert + enqueue path, with these
    differences from the URL path:

    - ``source_type = html_upload`` (Requirement 8.8).
    - ``name`` is the analyst's **required** name (not derived from a URL/title).
    - ``original_url``/``normalized_url`` are set from ``source_url`` **only when
      supplied**, otherwise left null — so an upload with no source URL does not
      participate in URL dedupe (Requirement 8.11).
    - **No** ``final_url``/``normalized_final_url`` and **no** snapshot copy
      (nothing was fetched; no live render — Requirement 8.10).
    - ``status_detail`` carries the ``requested`` event and the viability verdict
      exactly as the URL path (Requirements 8.9, 3.11).

    Failure cleanup is identical to :func:`create_from_check`: any partial
    permanent objects are deleted and no half-created row is left behind
    (Requirement 4.5). The enqueue of ``{dataset_id, data_version: 1}`` is
    identical too. There is no unique-URL race fallback: an upload without a
    source URL never inserts a normalized URL, and an upload *with* a tracked
    source URL is routed to refresh by :func:`_add_one` before this is reached
    (so this is only ever called for a brand-new or source-URL-less upload).

    Args:
        check_id: The Check Session the item belongs to.
        item: The completed, viable HTML-upload Check item (carries ``upload_id``
            and the stored verdict/plan location).
        name: The analyst-supplied dataset name (required for an HTML upload).
        source_url: The analyst's optional original page URL, stored for display
            and duplicate matching only; never fetched.
        description: Optional analyst-supplied source description.

    Returns:
        An :class:`AddResult`: ``created`` with the new dataset's id.

    Raises:
        ValueError: If ``name`` is missing or blank (an HTML upload requires a
            name — Requirement 8.8).
    """
    if name is None or not name.strip():
        raise ValueError("an HTML-upload dataset requires a non-empty name")

    dataset_id = _new_dataset_id()

    # Destination keys (every key from storage.keys). v1 is the first version.
    # The uploaded HTML + plan live under the SAME check keys a URL capture uses
    # (the Check handler wrote them there); there is no snapshot for an upload.
    page_dst = keys.dataset_raw_page(dataset_id, 1, 1)
    plan_dst = keys.dataset_raw_plan(dataset_id, 1)

    src_page = keys.check_page(check_id, item.item_id)
    src_plan = keys.check_plan(check_id, item.item_id)

    copied: list[str] = []
    try:
        s3.copy_object(src_page, page_dst)
        copied.append(page_dst)
        s3.copy_object(src_plan, plan_dst)
        copied.append(plan_dst)

        _insert_html_upload_dataset(dataset_id, item, name.strip(), source_url, description)
    except Exception:
        # Requirement 4.5: delete any partial permanent objects and do not
        # leave a half-created row. The INSERT is one transaction so a failed
        # INSERT rolled itself back; only the copied S3 objects remain.
        logger.exception(
            "create_from_html_upload failed for check %s item %s; cleaning up %d object(s)",
            check_id,
            item.item_id,
            len(copied),
        )
        _cleanup_partial(copied, check_id, item.item_id)
        raise

    # Hand the new version to review-analysis (IDs only; FIFO per dataset).
    enqueue(
        get_settings().processing_queue_url,
        {"dataset_id": dataset_id, "data_version": 1},
        message_group_id=dataset_id,
        # The processing FIFO queue has content-based dedup OFF (api-stack.ts),
        # so the producer MUST supply the dedup id. Keyed dataset_id:version.
        message_deduplication_id=f"{dataset_id}:1",
    )

    return AddResult(item.item_id, "created", dataset_id=dataset_id)


def _insert_html_upload_dataset(
    dataset_id: str,
    item: CheckItem,
    name: str,
    source_url: str | None,
    description: str | None,
) -> None:
    """Insert the ``html_upload`` ``datasets`` row and its v1 version row.

    Mirrors :func:`_insert_dataset` for the HTML-upload case: the row is born
    ``requested`` with a ``requested`` event in ``status_detail`` carrying the
    viability verdict (Requirements 8.9, 3.11), ``source_type = html_upload``,
    the analyst's required ``name``, and the optional source ``description`` on
    the event. ``original_url``/``normalized_url`` are set from ``source_url``
    **only when supplied** (else null, so the upload stays out of URL dedupe —
    Requirement 8.11); ``final_url``/``normalized_final_url`` are always null and
    no snapshot is produced. Both inserts share one transaction so a committed
    dataset always has its v1 row.
    """
    verdict = item.verdict or {}
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    # Normalize the source URL for duplicate matching only when one was given.
    normalized_url: str | None = None
    if source_url is not None and source_url.strip():
        from app.ingestion.url_normalizer import normalize

        normalized_url = normalize(source_url)
    else:
        source_url = None

    page_title = _page_title(verdict)
    platform = _platform_from_url(source_url)

    status_detail: dict[str, Any] = {
        "events": [
            {
                "status": DatasetStatus.REQUESTED.value,
                "at": now_iso,
                "message": "requested",
                "data": {
                    "source": "html_upload",
                    "description": description,
                    "viability": verdict,
                },
            }
        ],
        "description": description,
        "viability": verdict,
    }

    with session_scope() as session:
        session.execute(
            text(
                """
                INSERT INTO datasets (
                    id, name, page_title, source_type,
                    original_url, final_url, normalized_url, normalized_final_url,
                    platform, status, status_detail,
                    requested_at, updated_at, data_version, active_version
                )
                VALUES (
                    CAST(:id AS uuid), :name, :page_title,
                    CAST(:source_type AS source_type),
                    :original_url, NULL, :normalized_url, NULL,
                    :platform, CAST(:status AS dataset_status),
                    CAST(:status_detail AS jsonb),
                    :requested_at, :updated_at, 1, NULL
                )
                """
            ).bindparams(
                id=dataset_id,
                name=name,
                page_title=page_title,
                source_type=SourceType.HTML_UPLOAD.value,
                original_url=source_url,
                normalized_url=normalized_url,
                platform=platform,
                status=DatasetStatus.REQUESTED.value,
                status_detail=json.dumps(status_detail),
                requested_at=now,
                updated_at=now,
            ),
        )
        session.execute(
            text(
                """
                INSERT INTO dataset_versions (dataset_id, version, trigger, requested_at)
                VALUES (CAST(:id AS uuid), 1, :trigger, :requested_at)
                """
            ).bindparams(id=dataset_id, trigger=_INITIAL_TRIGGER, requested_at=now),
        )


def _cleanup_partial(copied: list[str], check_id: str, item_id: str) -> None:
    """Best-effort delete of the permanent objects copied so far (Req 4.5).

    Used by every ``create_from_check`` failure path (the unique-constraint race
    and any other error) so a losing dataset id never leaves orphaned permanent
    objects behind.
    """
    if copied:
        logger.info(
            "create_from_check cleaning up %d partial object(s) for check %s item %s",
            len(copied),
            check_id,
            item_id,
        )
    for key in copied:
        try:
            s3.delete_object(key)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            logger.warning("failed to delete partial object %s during cleanup", key)


def _is_duplicate_url_violation(exc: IntegrityError) -> bool:
    """True when *exc* is the ``uq_datasets_normalized_url`` unique violation.

    We distinguish this specific race (a concurrent Add of the same normalized
    URL) from any other integrity error by the constraint/index name in the
    error text, so unrelated ``IntegrityError``\\ s (a missing NOT NULL, a bad
    foreign key, a different unique index) are **not** swallowed. The index name
    appears in both the psycopg message (``...violates unique constraint
    "uq_datasets_normalized_url"``) and the Data API message, so matching on it
    works for both the local and AWS backends.
    """
    return _UNIQUE_URL_INDEX in str(exc.orig if exc.orig is not None else exc)


def _fallback_to_winner(check_id: str, item: CheckItem) -> AddResult:
    """Refresh the dataset a concurrent Add created first for this URL.

    Looks the winner up by the item's normalized Target/Final URL (the same
    lookup the Check uses, :func:`app.ingestion.duplicates.find_existing`) and
    routes to the Refresh Service with trigger ``duplicate_submission`` — the
    identical path an *already-tracked* URL takes — so exactly one dataset
    exists for the URL afterward (Property 6).
    """
    normalized_final = _normalized_final(item)
    winner = duplicates.find_existing(item.normalized, normalized_final)
    if winner is None:
        # The winner should exist (the unique index just rejected our insert for
        # it). If it has somehow gone (a delete between the violation and this
        # lookup), surface the original condition rather than pretending success.
        raise RuntimeError(
            f"unique violation on {_UNIQUE_URL_INDEX} but no winning dataset found "
            f"for normalized URL {item.normalized!r}"
        )

    capture = _check_capture(check_id, item)
    outcome = refresh_service.refresh(
        winner.id,
        trigger="duplicate_submission",
        capture=capture,
    )
    return AddResult(item.item_id, outcome, dataset_id=winner.id)


def _insert_dataset(dataset_id: str, item: CheckItem) -> None:
    """Insert the ``datasets`` row and its v1 ``dataset_versions`` row.

    Both inserts happen in one transaction so a committed dataset always has its
    version-1 row. The dataset is born ``requested`` with its ``status_detail``
    already populated (viability + redirects + the ``requested`` event), so the
    new row is complete without a follow-up ``db.status.transition`` — every
    *later* status change still flows through ``db.status``.

    A concurrent Add of the same *new* normalized URL makes the second INSERT
    violate ``uq_datasets_normalized_url`` and raise ``IntegrityError``;
    :func:`create_from_check` catches that specific violation and falls back to
    refreshing the winner's dataset (task 7.2). This function performs the plain
    insert and lets the ``IntegrityError`` propagate.
    """
    verdict = item.verdict or {}
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    name = _dataset_name(item, verdict)
    page_title = _page_title(verdict)
    final_url = item.final_url or item.input
    platform = _platform_from_url(final_url)

    # The requested event records the timestamp, the redirect details, and the
    # viability result (Requirement 5.2). Redirect hops are also kept under
    # status_detail.redirects (Requirement 2.5). The verdict is stored under
    # status_detail.viability so review-analysis can compare prediction with the
    # actual result (Requirement 3.11).
    status_detail: dict[str, Any] = {
        "events": [
            {
                "status": DatasetStatus.REQUESTED.value,
                "at": now_iso,
                "message": "requested",
                "data": {
                    "redirects": item.hops,
                    "viability": verdict,
                },
            }
        ],
        "redirects": item.hops,
        "viability": verdict,
    }

    with session_scope() as session:
        session.execute(
            text(
                """
                INSERT INTO datasets (
                    id, name, page_title, source_type,
                    original_url, final_url, normalized_url, normalized_final_url,
                    platform, status, status_detail,
                    requested_at, updated_at, data_version, active_version
                )
                VALUES (
                    CAST(:id AS uuid), :name, :page_title, CAST(:source_type AS source_type),
                    :original_url, :final_url, :normalized_url, :normalized_final_url,
                    :platform, CAST(:status AS dataset_status), CAST(:status_detail AS jsonb),
                    :requested_at, :updated_at, 1, NULL
                )
                """
            ).bindparams(
                id=dataset_id,
                name=name,
                page_title=page_title,
                source_type=SourceType.URL.value,
                original_url=item.input,
                final_url=final_url,
                normalized_url=item.normalized,
                normalized_final_url=_normalized_final(item),
                platform=platform,
                status=DatasetStatus.REQUESTED.value,
                status_detail=json.dumps(status_detail),
                requested_at=now,
                updated_at=now,
            ),
        )
        session.execute(
            text(
                """
                INSERT INTO dataset_versions (dataset_id, version, trigger, requested_at)
                VALUES (CAST(:id AS uuid), 1, :trigger, :requested_at)
                """
            ).bindparams(id=dataset_id, trigger=_INITIAL_TRIGGER, requested_at=now),
        )


# ---------------------------------------------------------------------------
# Upload → create a dataset from a staged upload (Requirement 7.4)
# ---------------------------------------------------------------------------


def create_from_upload(
    upload_id: str,
    name: str,
    mapping: dict[str, str],
    description: str | None = None,
) -> str:
    """Create a new upload dataset (v1) from a staged, previewed upload.

    Implements the ``ingestion.service.create_from_upload`` design bullet and
    Requirement 7.4: a submitted, valid upload stores the file and the confirmed
    column mapping under the new dataset, inserts a ``datasets`` row with
    ``source_type = upload`` and the analyst's required name (plus an optional
    source description), a ``dataset_versions`` v1 row, status ``requested``,
    and enqueues processing.

    Steps:

    1. **Re-validate** the staged file by re-parsing it through
       :func:`app.ingestion.upload_parser.preview_upload`. The analyst saw a
       preview at submit time, but the file is re-read here so a dataset is
       never created from a now-invalid or vanished object. ``preview_upload``
       deletes the staged object on an invalid file and raises
       :class:`~app.ingestion.upload_parser.UploadInvalidError`, which this
       function lets propagate so the endpoint returns ``422`` (Requirement 7.3).
    2. **Generate a dataset id** so the permanent S3 keys are known before the
       INSERT (same pattern as :func:`create_from_check`).
    3. **Copy** ``uploads/{upload_id}/file`` →
       ``datasets/{id}/raw/v1/upload.csv`` (keys from :mod:`app.storage.keys`).
    4. **Write** ``datasets/{id}/raw/v1/mapping.json`` with the confirmed
       mapping plus the keep rule and keep count from the re-validation preview
       (design: "mapping plus keep rule"), so review-analysis knows which column
       is which and which rows to keep (Requirement 7.6).
    5. **Insert** the ``datasets`` row (``source_type = upload``, ``name``
       required, URL columns null, no snapshot — Requirement 7.5) born
       ``requested`` with a ``requested`` event, and a ``dataset_versions`` v1
       row with trigger ``initial`` — both in one transaction.
    6. **Enqueue** ``{dataset_id, data_version: 1}`` onto the FIFO processing
       queue (one message group per dataset).

    On any failure while copying objects or inserting the record, the partial
    permanent objects are deleted and no half-created dataset is left behind
    (Requirement 4.5 analog for uploads).

    Args:
        upload_id: The staged upload's id (from ``POST /uploads``).
        name: The analyst-supplied dataset name (required, non-empty — the route
            validates non-emptiness before calling this).
        mapping: The confirmed canonical-field → header-name column mapping.
        description: Optional analyst-supplied source description.

    Returns:
        The new dataset's id.

    Raises:
        UploadInvalidError: The staged file is over the size limit, unparseable,
            or has no usable review-text column. The staged object has been
            deleted (by ``preview_upload``) before this propagates.
    """
    # 1. Re-validate: re-parse the staged object. On an invalid file this raises
    #    UploadInvalidError (and preview_upload has already deleted the object).
    preview = upload_parser.preview_upload(upload_id)

    # 2. Generate the dataset id so the destination keys are known up front.
    dataset_id = _new_dataset_id()

    upload_src = keys.upload_file(upload_id)
    upload_dst = keys.dataset_raw_upload(dataset_id, 1)
    mapping_dst = keys.dataset_raw_mapping(dataset_id, 1)

    copied: list[str] = []
    try:
        # 3. Copy the uploaded file into the dataset's permanent location.
        s3.copy_object(upload_src, upload_dst)
        copied.append(upload_dst)

        # 4. Write mapping.json = confirmed mapping + the keep rule/count.
        mapping_doc = _mapping_document(mapping, preview)
        s3.put_bytes(
            mapping_dst,
            json.dumps(mapping_doc).encode("utf-8"),
            content_type="application/json",
        )
        copied.append(mapping_dst)

        # 5. Insert the datasets row + v1 dataset_versions row (one transaction).
        _insert_upload_dataset(dataset_id, name, description, mapping, preview)
    except Exception:
        # Requirement 4.5 analog: delete partial permanent objects; the INSERT
        # is one transaction so a failed INSERT rolled itself back.
        logger.exception(
            "create_from_upload failed for upload %s; cleaning up %d object(s)",
            upload_id,
            len(copied),
        )
        _cleanup_partial(copied, upload_id, "upload")
        raise

    # 6. Hand the new version to review-analysis (IDs only; FIFO per dataset).
    enqueue(
        get_settings().processing_queue_url,
        {"dataset_id": dataset_id, "data_version": 1},
        message_group_id=dataset_id,
        # The processing FIFO queue has content-based dedup OFF (api-stack.ts),
        # so the producer MUST supply the dedup id. Keyed dataset_id:version so a
        # re-enqueue of the same version is de-duplicated (design "FIFO dedup").
        message_deduplication_id=f"{dataset_id}:1",
    )

    return dataset_id


def _mapping_document(mapping: dict[str, str], preview: UploadPreview) -> dict[str, Any]:
    """Build the ``mapping.json`` body: confirmed mapping plus the keep rule.

    The design says ``mapping.json`` stores the "mapping plus keep rule". The
    keep rule must follow the analyst's **confirmed** ``mapping``, not the
    parser's auto-suggested one: when the analyst maps a date column whose header
    the synonym table did not auto-detect (e.g. ``when``), the preview's
    ``keep_rule`` is still ``first_in_file`` even though a date column IS mapped.
    Deriving it from the confirmed mapping via
    :func:`app.ingestion.upload_parser.keep_rule_for` fixes that (Requirement
    7.6). ``will_keep``/``usable_rows`` are properties of the *file*, so they
    still come from the re-validation preview (``will_keep = min(usable_rows,
    MAX_REVIEWS)``).
    """
    return {
        "mapping": mapping,
        "keep_rule": upload_parser.keep_rule_for(mapping),
        "will_keep": preview.will_keep,
        "usable_rows": preview.usable_rows,
    }


def _insert_upload_dataset(
    dataset_id: str,
    name: str,
    description: str | None,
    mapping: dict[str, str],
    preview: UploadPreview,
) -> None:
    """Insert the upload ``datasets`` row and its v1 ``dataset_versions`` row.

    Mirrors :func:`_insert_dataset` for the upload case: the row is born
    ``requested`` with a ``requested`` event in ``status_detail`` (Requirement
    7.4), ``source_type = upload``, the URL columns null, and no snapshot
    (Requirement 7.5). The optional source ``description`` is stored on the
    ``requested`` event's ``data`` so it travels with the dataset without a new
    column. Both inserts share one transaction so a committed dataset always has
    its v1 row.

    The ``requested`` event records the analyst's **confirmed** ``mapping`` and
    the keep rule derived from it (via :func:`_mapping_document`), so the event
    and ``mapping.json`` agree (Requirement 7.6) — using the preview's
    auto-suggested mapping here would have disagreed with ``mapping.json`` for a
    date column the synonym table did not auto-detect. ``usable_rows`` and
    ``will_keep`` are file properties and still come from the preview.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    status_detail: dict[str, Any] = {
        "events": [
            {
                "status": DatasetStatus.REQUESTED.value,
                "at": now_iso,
                "message": "requested",
                "data": {
                    "source": "upload",
                    "description": description,
                    "mapping": mapping,
                    "keep_rule": upload_parser.keep_rule_for(mapping),
                    "usable_rows": preview.usable_rows,
                    "will_keep": preview.will_keep,
                },
            }
        ],
        "description": description,
    }

    with session_scope() as session:
        session.execute(
            text(
                """
                INSERT INTO datasets (
                    id, name, page_title, source_type,
                    original_url, final_url, normalized_url, normalized_final_url,
                    platform, status, status_detail,
                    requested_at, updated_at, data_version, active_version
                )
                VALUES (
                    CAST(:id AS uuid), :name, NULL, CAST(:source_type AS source_type),
                    NULL, NULL, NULL, NULL,
                    NULL, CAST(:status AS dataset_status), CAST(:status_detail AS jsonb),
                    :requested_at, :updated_at, 1, NULL
                )
                """
            ).bindparams(
                id=dataset_id,
                name=name,
                source_type=SourceType.UPLOAD.value,
                status=DatasetStatus.REQUESTED.value,
                status_detail=json.dumps(status_detail),
                requested_at=now,
                updated_at=now,
            ),
        )
        session.execute(
            text(
                """
                INSERT INTO dataset_versions (dataset_id, version, trigger, requested_at)
                VALUES (CAST(:id AS uuid), 1, :trigger, :requested_at)
                """
            ).bindparams(id=dataset_id, trigger=_INITIAL_TRIGGER, requested_at=now),
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _new_dataset_id() -> str:
    """Return a fresh dataset UUID.

    Generated in the application (rather than relying on the DB default) so the
    S3 destination keys are known *before* the INSERT — the capture is copied
    into ``datasets/{id}/`` first, and the row references the same id.
    """
    import uuid

    return str(uuid.uuid4())


def _verdict_label(item: CheckItem) -> str | None:
    """Return the item's verdict label (``will_work`` / ``limited`` / …) or None.

    A ``None`` label means the item never produced a verdict (for example it is
    still ``pending``/``checking``, ``invalid``, or in ``error``); such an item
    is not addable and is treated as ``wont_work`` by the caller's refuse path.
    """
    if not item.verdict:
        return None
    label = item.verdict.get("verdict")
    return label if isinstance(label, str) else None


def _dataset_name(item: CheckItem, verdict: dict[str, Any]) -> str:
    """Pick a human-readable dataset name.

    Prefers the page ``<title>`` (stored on the verdict evidence as
    ``page_title``), then the Locator's entity hint if the plan carried one,
    then the final/target URL host, and finally the raw input — so the Library
    always has something meaningful to show (Requirement 5.1).
    """
    title = _page_title(verdict)
    if title:
        return title
    host = _host(item.final_url or item.input)
    return host or item.input


def _page_title(verdict: dict[str, Any]) -> str | None:
    """Return the page title captured in the verdict evidence, if any."""
    evidence = verdict.get("evidence")
    if isinstance(evidence, dict):
        title = evidence.get("page_title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None


def _platform_from_url(url: str | None) -> str | None:
    """Return the platform (host without a leading ``www.``) for a URL.

    This is a lightweight label for the Library (for example ``g2.com``), not an
    identity used for matching — duplicate matching uses the normalized URL.
    """
    host = _host(url)
    if host is None:
        return None
    return host[4:] if host.startswith("www.") else host


def _host(url: str | None) -> str | None:
    """Return the lowercase host of *url*, or ``None`` when it has none."""
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _normalized_final(item: CheckItem) -> str | None:
    """Return the normalized final URL to store, falling back to the target.

    The duplicate lookup normalized the final URL at check time; the item keeps
    its normalized *target* in ``normalized``. When the probe produced no
    distinct final URL, the target's normalized form is the final one too, so a
    later check of the final URL still matches this dataset (Requirement 6.2).
    """
    from app.ingestion.url_normalizer import normalize

    if item.final_url:
        return normalize(item.final_url)
    return item.normalized


def _check_capture(check_id: str, item: CheckItem) -> CheckCapture:
    """Build the :class:`CheckCapture` naming this item's S3 artifacts.

    Every key comes from :mod:`app.storage.keys`; the snapshot is offered and
    the Refresh Service copies it only when the object exists.
    """
    return CheckCapture(
        page_key=keys.check_page(check_id, item.item_id),
        plan_key=keys.check_plan(check_id, item.item_id),
        snapshot_key=keys.check_snapshot(check_id, item.item_id),
    )


def _reapplied_outcome(item: CheckItem) -> Outcome:
    """Outcome to report for an item that is (or was just) ``applied`` by a race.

    Without the winning Add's return value we report the state the item most
    likely produced: ``already_refreshing`` when it mapped to an existing
    dataset (a refresh is in flight or just started), otherwise ``created`` for
    a new dataset. This keeps a double-submitted Add idempotent while still
    telling the analyst a dataset exists.
    """
    if item.existing_dataset:
        return "already_refreshing"
    return "created"
