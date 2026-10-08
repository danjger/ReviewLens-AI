"""Completion and failure stage of the processing pipeline (review-analysis task 6).

This is the pipeline's final stage (design flowchart nodes ``Z`` → ``OK`` /
``F``). After the metrics-and-output stage (task 5) has written
``reviews/v{n}.json`` and computed the ``metrics`` dict, this stage ends the
version:

* **Success (≥ 1 review).** Transition the dataset to ``updated``, set
  ``updated_at`` (the transition does this), set ``active_version`` to this
  version, write the ``metrics`` column, append a completion event with the
  review count and duration, and complete the ``dataset_versions`` row with
  ``outcome = 'updated'`` (Requirements 6.1, 6.3, 8.1).
* **Zero reviews.** A *normal* completion that ends in ``failed`` with the
  message "No reviews found on the captured pages." — not an exception. The
  ``active_version`` and ``metrics`` column are left untouched, so a failed
  refresh keeps serving the last good version (Requirements 6.2, 6.5).
* **Final-retry failure.** When an upstream stage raised and this is the last
  delivery (``MessageMeta.is_final_attempt``), the pipeline catches the error
  and ends the version in ``failed`` with a user-safe message: the AI-unavailable
  message for :class:`~app.extraction.errors.AIUnavailable`, a generic message
  otherwise (Requirements 7.2, 7.4). On a non-final attempt the error propagates
  so SQS retries (that branch lives in the handler, not here).

On **every** run — success or failure — the stage records the actual result next
to the Viability Check prediction in ``status_detail.viability.actual`` so the
prediction can be compared with what happened (Requirement 6.4), and completes
the ``dataset_versions`` row with ``completed_at``, ``review_count``,
``extraction_method``, and ``outcome`` (Requirement 6.3).

Design "Error Handling" (verbatim constraints honoured here):

* ``datasets.active_version`` is the latest version that finished ``updated``;
  on success ``active_version = data_version``, on failure it is unchanged.
* The ``metrics`` column is written only when a version finishes ``updated``, so
  a failed refresh never replaces good metrics.

Steering honoured:

* **Every status change through** :func:`app.db.status.transition` — which
  refreshes ``updated_at`` and publishes ``dataset.status.changed`` (Req 8.1).
* **DB only through** :func:`app.core.db.session_scope`.
* **Idempotent.** The ``dataset_versions`` completion is a conditional claim
  (``outcome IS NULL``) mirroring :func:`app.jobs.sweep.fail_version`, so a
  redelivery or a racing sweeper/DLQ completes the row exactly once (Property 5,
  Requirement 7.3). ``active_version`` and the ``metrics`` column are written
  under the dataset row lock, monotonically — ``active_version`` only ever moves
  forward (``GREATEST``-style guard), so re-running an older version never pulls
  it back (Property 4). The ``viability.actual`` write and the status event are
  deterministic in the version's outputs, so running twice leaves identical
  stored state and one completion.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from app.core.db import session_scope
from app.db.models import DatasetStatus
from app.db.status import transition

logger = logging.getLogger(__name__)

#: Message shown when a version completes with no reviews (Requirement 6.2,
#: verbatim).
ZERO_REVIEWS_MESSAGE = "No reviews found on the captured pages."

#: Message shown when retries are exhausted and the AI was unavailable
#: (Requirement 7.4, verbatim; the em dash is intentional).
AI_UNAVAILABLE_MESSAGE = "AI service unavailable — try refreshing later."

#: Generic user-safe message for any other error on the final attempt
#: (Requirement 7.2). It names no internals.
GENERIC_FAILURE_MESSAGE = "Processing failed. Try refreshing this dataset."

#: Message suffix appended to the failure event when a *refresh* (version ≥ 2)
#: fails, so the event says the dataset keeps serving the last good version
#: (Requirement 6.5).
REFRESH_KEPT_SUFFIX = " The previous reviews are still available."


class Outcome(enum.StrEnum):
    """Terminal outcome of a processing run.

    Stored in ``dataset_versions.outcome`` and used as the ``failed`` /
    ``updated`` status. The two values match the design's ``outcome`` column
    vocabulary.
    """

    UPDATED = "updated"
    FAILED = "failed"


@dataclass(frozen=True)
class CompletionDecision:
    """The pure decision for how a run ends, independent of the database.

    Keeping the decision a pure function of a few facts (see
    :func:`decide_completion`) means every branch — success, zero reviews, and
    both final-retry failures — has a unit test, and Property 4 can reason about
    ``active_version`` without a database.

    Attributes:
        outcome: ``updated`` on success, ``failed`` otherwise.
        status: The :class:`DatasetStatus` to transition to (mirrors
            ``outcome``).
        message: The event/completion message. User-safe for every failure
            path.
        set_active: Whether this version should become ``active_version`` — only
            true on a successful (``updated``) run (design "Error Handling": on
            failure ``active_version`` stays unchanged).
        write_metrics: Whether the ``metrics`` column should be written — only
            true on success (design: the ``metrics`` column is written only when
            a version finishes ``updated``).
    """

    outcome: Outcome
    status: DatasetStatus
    message: str
    set_active: bool
    write_metrics: bool


def decide_completion(
    *,
    review_count: int,
    error: BaseException | None,
    is_refresh: bool,
    ai_unavailable: bool = False,
) -> CompletionDecision:
    """Decide how a run ends, from pure inputs (no database, no I/O).

    The caller resolves the inputs: ``review_count`` from the metrics stage,
    ``error`` and ``ai_unavailable`` from a caught upstream exception on the
    final attempt, and ``is_refresh`` from the version number (``version >= 2``).
    An ``error`` is only ever passed on the final attempt — a non-final error
    propagates in the handler so SQS retries and never reaches here.

    Decision order:

    1. **An error (final attempt)** ends the version in ``failed`` with a
       user-safe message — the AI-unavailable message when the error was an AI
       outage (Requirement 7.4), otherwise the generic message (Requirement
       7.2).
    2. **Zero reviews** (no error) is a normal completion ending in ``failed``
       with the zero-reviews message (Requirement 6.2).
    3. **At least one review** ends the version in ``updated`` (Requirement
       6.1): this version becomes ``active_version`` and the ``metrics`` column
       is written.

    A failing *refresh* (version ≥ 2) appends a note to the message that the
    previous reviews are still served (Requirement 6.5); ``active_version`` and
    the ``metrics`` column are left unchanged for every failure, which is what
    keeps the last good version live.

    Args:
        review_count: Number of stored reviews for this version.
        error: The upstream exception caught on the final attempt, or ``None``.
        is_refresh: Whether this is a refresh (data version ≥ 2).
        ai_unavailable: Whether *error* is (or wraps) an AI-provider outage.

    Returns:
        The :class:`CompletionDecision` describing the outcome, status, message,
        and whether to advance ``active_version`` / write ``metrics``.
    """
    if error is not None:
        # Final-attempt failure (the only time an error reaches here). The
        # message never names internals (Requirement 7.2).
        base = AI_UNAVAILABLE_MESSAGE if ai_unavailable else GENERIC_FAILURE_MESSAGE
        message = base + REFRESH_KEPT_SUFFIX if is_refresh else base
        return CompletionDecision(
            outcome=Outcome.FAILED,
            status=DatasetStatus.FAILED,
            message=message,
            set_active=False,
            write_metrics=False,
        )

    if review_count <= 0:
        # Normal completion with nothing extracted (Requirement 6.2).
        message = ZERO_REVIEWS_MESSAGE + REFRESH_KEPT_SUFFIX if is_refresh else ZERO_REVIEWS_MESSAGE
        return CompletionDecision(
            outcome=Outcome.FAILED,
            status=DatasetStatus.FAILED,
            message=message,
            set_active=False,
            write_metrics=False,
        )

    # Success (Requirement 6.1).
    return CompletionDecision(
        outcome=Outcome.UPDATED,
        status=DatasetStatus.UPDATED,
        message="updated",
        set_active=True,
        write_metrics=True,
    )


@dataclass(frozen=True)
class ActualResult:
    """The actual processing result recorded next to the viability prediction.

    Written to ``status_detail.viability.actual`` (Requirement 6.4) so the
    summary can compare what the Viability Check predicted with what happened.

    Attributes:
        reviews: Number of reviews extracted (stored).
        pages: Number of pages captured.
        method: The extraction method actually used (``metrics.extraction.method``).
        fallbacks: Number of pages that fell back to the Review Locator.
    """

    reviews: int
    pages: int
    method: str
    fallbacks: int

    def as_dict(self) -> dict[str, Any]:
        """Render for the JSONB ``status_detail.viability.actual`` block."""
        return {
            "reviews": self.reviews,
            "pages": self.pages,
            "method": self.method,
            "fallbacks": self.fallbacks,
        }


def next_active_version(
    current: int | None, version: int, decision: CompletionDecision
) -> int | None:
    """Return ``active_version`` after a version completes (pure, Property 4).

    This is the exact rule the ``active_version`` SQL in :func:`complete` applies,
    lifted out so it can be reasoned about and property-tested without a
    database:

    * On a **failed** version (``decision.set_active`` is ``False``) the active
      version is unchanged, so a failed refresh keeps serving the last good
      version (Requirement 6.5, design "Error Handling").
    * On a **successful** version it advances **monotonically** —
      ``max(current, version)`` — so an out-of-order re-run of an older
      successful version never pulls ``active_version`` backward (Property 4).

    Args:
        current: The dataset's current ``active_version`` (``None`` if no version
            has ever succeeded).
        version: The version that just completed.
        decision: Its :class:`CompletionDecision`.

    Returns:
        The ``active_version`` the dataset should hold after this completion.
    """
    if not decision.set_active:
        return current
    return max(current or 0, version)


def complete(
    *,
    dataset_id: str,
    version: int,
    decision: CompletionDecision,
    metrics: dict[str, Any],
    actual: ActualResult,
    review_count: int,
    duration_ms: int,
) -> bool:
    """End a version: complete its row, update the dataset, and transition.

    The whole of task 6's database side. Mirrors
    :func:`app.jobs.sweep.fail_version`'s conditional-claim pattern so the
    completion is idempotent against redelivery and racing sweeper/DLQ writers.
    In one transaction (under the dataset row lock) it:

    1. **Claims** the ``dataset_versions`` row by setting ``completed_at``,
       ``review_count``, ``extraction_method``, and ``outcome`` **only while it
       is still in flight** (``outcome IS NULL``), using ``RETURNING`` to learn
       whether this call won. A redelivery or a racing writer that already
       completed the row gets nothing back and bows out — exactly one completion
       per version (Property 5, Requirement 6.3).
    2. On success (``decision.set_active``), advances ``active_version`` to this
       version **monotonically** (only when greater than the current value, so
       an out-of-order re-run of an older version never pulls it back — Property
       4) and writes the ``metrics`` column. On failure, both are left unchanged
       (design "Error Handling"; Requirement 6.5).
    3. Records the actual result under ``status_detail.viability.actual`` for the
       prediction-vs-actual comparison (Requirement 6.4), creating the
       ``viability`` object if the Check never wrote one.

    After the claim is won and committed, the status transition is applied
    through :func:`app.db.status.transition` so the event, ``updated_at`` bump,
    and ``dataset.status.changed`` publish happen exactly once (Requirements 6.1,
    8.1). The ``metrics`` the event carries is read from the row the transition
    loads, which this function already wrote on the success path.

    Args:
        dataset_id: The dataset being completed.
        version: The data version to complete (expected in flight).
        decision: The :class:`CompletionDecision` from :func:`decide_completion`.
        metrics: The metrics dict from the metrics stage (written to the column
            only when ``decision.write_metrics``).
        actual: The actual result recorded next to the viability prediction.
        review_count: Reviews stored, written to ``dataset_versions.review_count``.
        duration_ms: Elapsed processing time, carried on the completion event.

    Returns:
        ``True`` when this call won the claim and performed the completion;
        ``False`` when the version was already completed (idempotent no-op).
    """
    extraction_method = str(metrics.get("extraction", {}).get("method", "")) or None

    with session_scope() as session:
        # Lock the dataset row so the version claim, the active_version/metrics
        # update, and the viability.actual write all serialise against any other
        # writer (a racing sweeper/DLQ, or a duplicate delivery).
        locked = session.execute(
            text("SELECT 1 FROM datasets WHERE id = CAST(:id AS uuid) FOR UPDATE").bindparams(
                id=dataset_id
            ),
        ).first()
        if locked is None:
            raise ValueError(f"Dataset {dataset_id!r} does not exist")

        # (1) Atomic claim of the version row (Requirement 6.3). Only one caller
        # flips outcome NULL → updated/failed; a replay gets no row back.
        claimed = session.execute(
            text(
                """
                UPDATE dataset_versions
                SET completed_at = now(),
                    review_count = :review_count,
                    extraction_method = :extraction_method,
                    outcome = :outcome
                WHERE dataset_id = CAST(:id AS uuid)
                  AND version = :version
                  AND outcome IS NULL
                RETURNING version
                """
            ).bindparams(
                id=dataset_id,
                version=version,
                review_count=review_count,
                extraction_method=extraction_method,
                outcome=decision.outcome.value,
            ),
        ).first()
        if claimed is None:
            logger.info(
                "complete: dataset %s v%d already completed by another writer; skipping",
                dataset_id,
                version,
            )
            return False

        # (2) Success only: advance active_version monotonically and write the
        # metrics column. GREATEST keeps active_version from moving backwards if
        # an older version is re-run out of order (Property 4). On failure both
        # are deliberately untouched so the last good version keeps serving
        # (Requirement 6.5 / design "Error Handling").
        if decision.set_active:
            session.execute(
                text(
                    """
                    UPDATE datasets
                    SET active_version = GREATEST(COALESCE(active_version, 0), :version),
                        metrics = CAST(:metrics AS jsonb)
                    WHERE id = CAST(:id AS uuid)
                    """
                ).bindparams(id=dataset_id, version=version, metrics=_json(metrics)),
            )

        # (3) Record actual vs predicted under status_detail.viability.actual
        # (Requirement 6.4). A single ``jsonb_set`` on the two-level path
        # ``{viability,actual}`` only creates the leaf when its parent object
        # already exists; when the Check never wrote a ``viability`` object
        # (e.g. uploads), it would be a silent no-op and the actual result would
        # be lost. So first ensure the ``viability`` parent exists (preserving
        # any ``prediction`` the Check wrote), then set ``actual`` under it.
        session.execute(
            text(
                """
                UPDATE datasets
                SET status_detail = jsonb_set(
                    jsonb_set(
                        COALESCE(status_detail, '{}'::jsonb),
                        '{viability}',
                        COALESCE(status_detail -> 'viability', '{}'::jsonb),
                        true
                    ),
                    '{viability,actual}',
                    :actual,
                    true
                )
                WHERE id = CAST(:id AS uuid)
                """
            ).bindparams(
                bindparam("actual", type_=JSONB),
                id=dataset_id,
            ),
            {"actual": actual.as_dict(), "id": dataset_id},
        )

    # Transition after winning the claim so the event + publish happen once
    # (Requirements 6.1, 8.1). The payload the transition emits reads the row we
    # just wrote, so a successful version publishes its new metrics and
    # active_version.
    transition(
        dataset_id,
        decision.status,
        decision.message,
        extra={
            "version": version,
            "review_count": review_count,
            "duration_ms": duration_ms,
        },
    )
    logger.info(
        "completed %s v%d → %s (%d review(s), %dms)",
        dataset_id,
        version,
        decision.outcome.value,
        review_count,
        duration_ms,
    )
    return True


def _json(value: dict[str, Any]) -> str:
    """Serialize *value* for a ``CAST(... AS jsonb)`` bind.

    Local import of :mod:`json` keeps the module's import block focused; the
    metrics dict is plain JSON-serialisable data from the metrics stage.
    """
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)
