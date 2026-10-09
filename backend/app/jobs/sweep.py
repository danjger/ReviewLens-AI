"""Sweeper job: recover datasets stuck in ``requested`` or ``processing``.

The sweeper runs every 5 minutes (EventBridge Scheduler → Lambda, or an ECS
scheduled task; design "The sweeper runs every 5 minutes …"). Each run claims
candidate rows with ``SELECT … FOR UPDATE SKIP LOCKED`` so several sweeper runs
can execute at once and never act on the same row (Requirement 1.4/1.5, the
steering "SKIP LOCKED for concurrent safety" rule). It performs two recoveries:

- **Re-enqueue stale ``requested`` rows** (Requirement 1.4). A dataset that has
  stayed ``requested`` longer than ``SWEEP_REQUESTED_AFTER_MIN`` minutes
  (default 5) never had its processing message picked up — the producer's
  enqueue may have been lost, or the message aged out. The sweeper publishes a
  fresh ``{dataset_id, data_version}`` message onto the FIFO processing queue
  (same body and ``MessageGroupId = dataset_id`` the Add and Refresh paths use),
  so the Worker's start guard then treats it as a normal start. No status change
  is made: the row is already ``requested``.

- **Fail stale ``processing`` rows** (Requirement 1.5). A dataset that has
  stayed ``processing`` with **no new progress event** for longer than
  ``SWEEP_PROCESSING_STALE_MIN`` minutes (default 20) — for example after a
  Lambda hard timeout killed the Worker mid-run — is moved to ``failed`` with
  the message "Processing stopped unexpectedly." and its in-flight
  ``dataset_versions`` row is completed with ``outcome = 'failed'`` (design "A
  DLQ consumer does the same ``failed`` transition").

Staleness is measured from the dataset's **last activity**: the ``at``
timestamp of the most recent ``status_detail`` event, falling back to
``updated_at`` when there are no events. This matters for the ``processing``
case because progress is recorded with :func:`app.db.status.log_event`, which
appends an event **without** bumping ``updated_at`` — so a Worker that is still
making progress keeps the row fresh even though ``updated_at`` is unchanged.

Engineering rules honoured:

- **Stateless / idempotent.** No correctness depends on process memory. Each
  candidate is claimed under a row lock and re-checked against the current wall
  clock inside the transaction, so two concurrent sweeps, or the same sweep run
  twice, converge on the same result. Re-enqueue is safe to repeat: the FIFO
  queue plus the Worker's start guard collapse duplicate messages. Failing a
  version is guarded on the row still being ``processing`` and the version row
  still being in flight, so a second sweep (or a racing DLQ consumer) is a
  no-op.
- **DB only through ``core.db``**; every status change through
  :func:`app.db.status.transition`; queue publishing through
  :func:`app.core.queue.enqueue`. Nothing here constructs an SQS or database
  client by hand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.queue import enqueue
from app.db.models import DatasetStatus
from app.db.status import transition

logger = logging.getLogger(__name__)

#: Message shown on the event and the failed version when a ``processing`` row
#: is swept because it stopped making progress (Requirement 1.5, verbatim).
STALE_PROCESSING_MESSAGE = "Processing stopped unexpectedly."

#: Trigger recorded alongside the re-enqueue so the event log shows the sweeper
#: acted (the ``dataset_versions.trigger`` column already anticipates this).
SWEEP_TRIGGER = "sweeper"


@dataclass(frozen=True)
class SweepResult:
    """What one :func:`run` call did, for logging and tests.

    Attributes:
        reenqueued: Dataset ids whose stale ``requested`` version was
            re-enqueued (Requirement 1.4).
        failed: Dataset ids whose stale ``processing`` version was moved to
            ``failed`` (Requirement 1.5).
    """

    reenqueued: list[str]
    failed: list[str]

    @property
    def total(self) -> int:
        """Total number of datasets acted on in this run."""
        return len(self.reenqueued) + len(self.failed)


def run(now: datetime | None = None) -> SweepResult:
    """Run one sweep pass and return what it did.

    Args:
        now: The reference time staleness is measured against. Defaults to the
            current UTC time; tests pass a fixed value so backdated rows have a
            deterministic age.

    Returns:
        A :class:`SweepResult` listing the datasets re-enqueued and failed.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)

    requested_cutoff = now - timedelta(minutes=settings.sweep_requested_after_min)
    processing_cutoff = now - timedelta(minutes=settings.sweep_processing_stale_min)

    reenqueued = _sweep_requested(requested_cutoff)
    failed = _sweep_processing(processing_cutoff)

    result = SweepResult(reenqueued=reenqueued, failed=failed)
    if result.total:
        logger.info(
            "sweep: re-enqueued %d stale requested, failed %d stale processing",
            len(reenqueued),
            len(failed),
        )
    return result


def lambda_entry(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ANN401
    """AWS Lambda entry point for the EventBridge Scheduler schedule.

    The sweeper runs every 5 minutes (``WorkersStack`` wires an EventBridge
    Scheduler schedule to this handler; its CMD is
    ``["app.jobs.sweep.lambda_entry"]``). The scheduler event carries nothing
    the sweeper needs — staleness is read from the database rows — so the event
    is ignored and :func:`run` is called with the current time. The returned
    counts land in the invocation logs. Concurrent invocations are safe because
    :func:`run` claims rows with ``FOR UPDATE SKIP LOCKED``.
    """
    result = run()
    return {
        "reenqueued": len(result.reenqueued),
        "failed": len(result.failed),
    }


# ---------------------------------------------------------------------------
# Stale `requested` → re-enqueue (Requirement 1.4)
# ---------------------------------------------------------------------------


def _sweep_requested(cutoff: datetime) -> list[str]:
    """Re-enqueue every ``requested`` dataset older than *cutoff*.

    Candidates are claimed with ``FOR UPDATE SKIP LOCKED`` inside one
    transaction and re-enqueued while the lock is held, so a second concurrent
    sweep skips the locked rows and never double-counts them. The actual
    duplicate-suppression safety net is the FIFO queue plus the Worker's start
    guard; the lock only keeps two sweeps from both doing the (idempotent) work.
    """
    reenqueued: list[str] = []
    queue_url = get_settings().processing_queue_url

    with session_scope() as session:
        for dataset_id, data_version in _claim_stale(
            session, DatasetStatus.REQUESTED.value, cutoff
        ):
            enqueue(
                queue_url,
                {"dataset_id": dataset_id, "data_version": data_version},
                message_group_id=dataset_id,
                # FIFO queue has content-based dedup OFF (api-stack.ts): supply
                # the dedup id so a re-enqueue of the same version is deduped.
                message_deduplication_id=f"{dataset_id}:{data_version}",
            )
            logger.info(
                "sweep: re-enqueued dataset %s v%d (stuck in requested)",
                dataset_id,
                data_version,
            )
            reenqueued.append(dataset_id)

    return reenqueued


# ---------------------------------------------------------------------------
# Stale `processing` → fail (Requirement 1.5)
# ---------------------------------------------------------------------------


def _sweep_processing(cutoff: datetime) -> list[str]:
    """Fail every ``processing`` dataset with no progress since *cutoff*.

    Each candidate is claimed under a row lock, then failed through
    :func:`fail_version`, which transitions the dataset to ``failed`` and
    completes its in-flight ``dataset_versions`` row. The claim and the fail run
    in separate transactions on purpose: the claim's ``SKIP LOCKED`` only keeps
    two concurrent sweeps from selecting the same row, while ``fail_version``'s
    own guards (status still ``processing``, version row still in flight) make
    the write idempotent even against a racing DLQ consumer.
    """
    failed: list[str] = []

    with session_scope() as session:
        claimed = _claim_stale(session, DatasetStatus.PROCESSING.value, cutoff)

    for dataset_id, data_version in claimed:
        if fail_version(dataset_id, data_version, STALE_PROCESSING_MESSAGE):
            logger.info(
                "sweep: failed dataset %s v%d (stuck in processing)",
                dataset_id,
                data_version,
            )
            failed.append(dataset_id)

    return failed


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _claim_stale(
    session: Session,
    status: str,
    cutoff: datetime,
) -> list[tuple[str, int]]:
    """Claim datasets in *status* whose last activity is at or before *cutoff*.

    "Last activity" is the ``at`` timestamp of the most recent
    ``status_detail`` event, falling back to ``updated_at`` when the event log
    is empty. Rows are locked with ``FOR UPDATE SKIP LOCKED`` so a concurrent
    sweep selecting at the same instant skips rows this transaction already
    holds, which is what stops two sweeps from acting on one row (steering:
    "SKIP LOCKED for concurrent safety").

    The comparison is done in SQL so only genuinely stale rows are locked. The
    most recent event's ``at`` is read from the last element of the
    ``status_detail -> 'events'`` JSONB array.

    Returns:
        ``(dataset_id, data_version)`` for each claimed row.
    """
    rows = session.execute(
        text(
            """
            SELECT id, data_version
            FROM datasets
            WHERE status = CAST(:status AS dataset_status)
              AND archived_at IS NULL
              AND COALESCE(
                    (
                        (status_detail -> 'events' -> -1 ->> 'at')::timestamptz
                    ),
                    updated_at
                  ) <= :cutoff
            ORDER BY updated_at
            FOR UPDATE SKIP LOCKED
            """
        ).bindparams(status=status, cutoff=cutoff),
    ).all()
    return [(str(r[0]), int(r[1])) for r in rows]


def fail_version(dataset_id: str, version: int, message: str) -> bool:
    """Move a dataset's in-flight version to ``failed`` and complete its row.

    Shared by the sweeper's stale-``processing`` recovery (Requirement 1.5) and
    the DLQ consumer (:mod:`app.handlers.dlq`), which both end a version that
    can no longer complete on its own. In one transaction this:

    1. Re-reads the dataset's status and ``data_version`` under a row lock and
       **guards** that the dataset is still ``processing`` for *version*. If the
       status already moved on (a late Worker finished it), the call is a no-op
       and returns ``False``.
    2. **Claims** the version by completing its ``dataset_versions`` row with
       ``completed_at`` and ``outcome = 'failed'`` **only when it is still in
       flight** (``outcome IS NULL``), using ``RETURNING`` to learn whether a
       row was flipped. That conditional update is the atomic idempotency gate:
       exactly one concurrent caller (a racing sweeper and DLQ consumer, or a
       duplicate delivery) gets a row back, so the losers return ``False``
       without a second transition (Requirement 6.3).

    The ``failed`` status transition itself is applied through
    :func:`app.db.status.transition` **after** this call wins the claim, so it
    appends the event, refreshes ``updated_at``, and publishes
    ``dataset.status.changed`` exactly once (Requirement 8.1). ``active_version``
    is deliberately left unchanged, so a failed refresh keeps serving the last
    good version (Requirement 6.5).

    Args:
        dataset_id: The dataset to fail.
        version: The data version expected to be in flight.
        message: User-safe failure message stored on the event and reflected to
            the UI.

    Returns:
        ``True`` when this call won the claim and performed the failure;
        ``False`` when the version was no longer an in-flight ``processing``
        version (another writer already handled it, or it completed normally).
    """
    with session_scope() as session:
        row = session.execute(
            text(
                "SELECT status, data_version FROM datasets WHERE id = CAST(:id AS uuid) FOR UPDATE"
            ).bindparams(id=dataset_id),
        ).first()
        if row is None:
            logger.warning("fail_version: dataset %s does not exist; skipping", dataset_id)
            return False

        status = str(row[0])
        data_version = int(row[1])

        # Status guard: only an in-flight `processing` version that matches the
        # message is a candidate. A row that already moved to updated/failed, or
        # whose data_version has advanced, is left untouched.
        if status != DatasetStatus.PROCESSING.value or data_version != version:
            logger.info(
                "fail_version: dataset %s v%d no longer an in-flight processing version "
                "(status=%s, data_version=%d); skipping",
                dataset_id,
                version,
                status,
                data_version,
            )
            return False

        # Atomic claim: complete the in-flight version row with a failed outcome
        # (Req 6.3) only while outcome IS NULL. ``RETURNING`` tells us whether a
        # row was flipped — it is the idempotency gate: exactly one concurrent
        # caller flips NULL → 'failed' (the dataset row is locked above, so the
        # two run strictly in order), and a replay or racing DLQ consumer gets
        # no row back and bows out. A dataset with no version row (nothing to
        # complete) likewise returns nothing and is skipped.
        claimed = session.execute(
            text(
                """
                UPDATE dataset_versions
                SET completed_at = now(),
                    outcome = 'failed'
                WHERE dataset_id = CAST(:id AS uuid)
                  AND version = :version
                  AND outcome IS NULL
                RETURNING version
                """
            ).bindparams(id=dataset_id, version=version),
        ).first()
        if claimed is None:
            logger.info(
                "fail_version: dataset %s v%d already completed by another writer; skipping",
                dataset_id,
                version,
            )
            return False

    # Transition after winning the claim so the event + publish happen once.
    transition(
        dataset_id,
        DatasetStatus.FAILED,
        message,
        extra={"version": version, "trigger": SWEEP_TRIGGER},
    )
    return True
