"""Dataset status transitions and the append-only ``status_detail`` event log.

Every change to a dataset's lifecycle status flows through this module
(Requirement 4.4). Two operations are offered:

- :func:`transition` – change the ``status`` column **and** append a matching
  event to ``status_detail.events`` in a single transaction, then publish a
  ``dataset.status.changed`` event to EventBridge.
- :func:`log_event` – append a progress event to ``status_detail.events``
  *without* changing the status column (for example "Fetching page 3 of 10").

Both operations are append-only and keep events in time order. Earlier events
are never rewritten, and after :func:`transition` the last *status* event in
the log equals the ``status`` column (Correctness Property 5).

Concurrency and idempotency
---------------------------
``status_detail`` is a JSONB document shared by every writer of a dataset. To
keep concurrent appends from clobbering one another the row is locked with
``SELECT ... FOR UPDATE`` before the append, and the new event is concatenated
onto ``status_detail -> 'events'`` with PostgreSQL's ``||`` operator inside the
same transaction. Because the whole read-modify-write happens under the row
lock, interleaved writers serialise and no event is lost, so the log stays in
time order even under concurrency (Requirement 4.4, Property 5).

The ``updated_at`` column is refreshed on every :func:`transition` so the
Library's "last updated" ordering reflects status changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.core.db import session_scope
from app.db.models import Dataset, DatasetStatus
from app.events.publisher import publish_event

#: EventBridge detail-type for a dataset status change.
STATUS_CHANGED_DETAIL_TYPE = "dataset.status.changed"


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _build_event(
    *,
    status: str | None,
    message: str,
    at: str | None = None,
    data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build one ``status_detail.events`` entry.

    The entry always carries ``status`` (``None`` for a progress-only event),
    ``at`` (ISO-8601 UTC), ``message``, and ``data`` (from ``extra``; ``{}``
    when not supplied).

    ``at`` is normally left ``None`` here and stamped by :func:`_append_event`
    *inside the row lock*, so an event's timestamp always matches the serialised
    append order (Correctness Property 5). An explicit ``at`` may be supplied
    for tests of the pure event shape.
    """
    return {
        "status": status,
        "at": at,
        "message": message,
        "data": data or {},
    }


def _append_event(session: Session, dataset_id: str, event: dict[str, Any]) -> None:
    """Append ``event`` to ``status_detail.events`` under a row lock.

    Locks the dataset row with ``SELECT ... FOR UPDATE`` so concurrent writers
    serialise, then stamps the event's ``at`` timestamp *while the lock is held*
    and concatenates the new event onto the existing events array with
    ``jsonb ||``. Because ``at`` is assigned under the lock, timestamp order
    always matches the (serialised) append order, so the events stay in time
    order even under concurrency (Correctness Property 5).

    The ``at`` value is written back onto the passed-in ``event`` dict in place,
    so callers (notably :func:`transition`) can read the final timestamp for the
    published payload and keep the stored and published ``at`` identical.
    """
    # Lock the row for the duration of the transaction so concurrent appends
    # to the same dataset serialise rather than overwrite each other.
    # Compare against the native ``uuid`` column with an explicit CAST so the
    # bound ``str`` id works under BOTH drivers. (An earlier note here assumed a
    # bare ``:id`` stays ``uuid = uuid`` on the Data API — it does not: the
    # aurora-data-api driver binds it as text and PostgreSQL rejects
    # ``uuid = text``. The CAST is the same fix applied across the raw-SQL paths
    # in platform-foundation task 15.)
    locked = session.execute(
        text("SELECT 1 FROM datasets WHERE id = CAST(:id AS uuid) FOR UPDATE"),
        {"id": dataset_id},
    ).first()
    if locked is None:
        raise ValueError(f"Dataset {dataset_id!r} does not exist")

    # Stamp ``at`` now, after winning the lock, so the timestamp order matches
    # the serialised append order. A writer that computes an earlier wall-clock
    # time but wins the lock later would otherwise store an out-of-order ``at``.
    event["at"] = _utc_now_iso()

    # Append the event to status_detail.events, creating the array if the key
    # is somehow absent. jsonb_set with the existing array || the new element
    # keeps all earlier events byte-for-byte unchanged.
    stmt = text(
        """
        UPDATE datasets
        SET status_detail = jsonb_set(
            status_detail,
            '{events}',
            COALESCE(status_detail -> 'events', '[]'::jsonb) || :event,
            true
        )
        WHERE id = CAST(:id AS uuid)
        """
    ).bindparams(
        bindparam("event", type_=JSONB),
    )
    # ``id`` is CAST to uuid in the SQL so the bound str compares against the
    # native ``uuid`` column under both the psycopg and Data API drivers.
    session.execute(stmt, {"event": [event], "id": dataset_id})


def log_event(
    dataset_id: str,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a progress event to ``status_detail.events`` without a status change.

    Use this to record progress within a status (for example page-by-page
    capture progress). The appended event has ``status=None`` so it does not
    affect the "last status event equals the status column" invariant.

    Args:
        dataset_id: The dataset whose log is appended to.
        message: Human-readable progress message.
        extra: Optional structured data stored under the event's ``data`` key.

    Raises:
        ValueError: If the dataset does not exist.
    """
    # ``at`` is stamped inside the row lock by ``_append_event`` so the
    # timestamp order matches the serialised append order (Property 5).
    event = _build_event(status=None, message=message, data=extra)
    with session_scope() as session:
        _append_event(session, dataset_id, event)


def transition(
    dataset_id: str,
    new_status: DatasetStatus,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Change a dataset's status and append a matching event, atomically.

    In one transaction this updates the ``status`` column, refreshes
    ``updated_at``, and appends a status event to ``status_detail.events``.
    After the transaction commits it publishes a ``dataset.status.changed``
    event to EventBridge carrying the new status and the dataset's version and
    metrics fields.

    Every status change in the system goes through this function
    (Requirement 4.4).

    Args:
        dataset_id: The dataset to transition.
        new_status: The new :class:`DatasetStatus`.
        message: Detail message stored on the appended event.
        extra: Optional structured data stored under the event's ``data`` key.

    Raises:
        ValueError: If the dataset does not exist.
    """
    # ``at`` is left unset here and stamped by ``_append_event`` under the row
    # lock, which writes the final value back onto ``event``. We then reuse that
    # exact value for the published payload so the stored and published ``at``
    # are identical.
    event = _build_event(status=new_status.value, message=message, data=extra)

    with session_scope() as session:
        # Lock + append first so the event array and the status column are
        # written under the same row lock in one transaction. This also stamps
        # ``event["at"]`` under the lock.
        _append_event(session, dataset_id, event)
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:  # pragma: no cover - _append_event already guards
            raise ValueError(f"Dataset {dataset_id!r} does not exist")
        dataset.status = new_status
        dataset.updated_at = datetime.now(UTC)

        # Capture the fields needed for the event payload while the row is
        # loaded; publish after commit so we never emit an event for a change
        # that rolled back. ``event["at"]`` is the under-lock timestamp
        # ``_append_event`` just stored, so the published ``at`` equals the
        # stored event's ``at``.
        payload = {
            "dataset_id": dataset_id,
            "status": new_status.value,
            "at": event["at"],
            "data_version": dataset.data_version,
            "active_version": dataset.active_version,
            "message": message,
            "metrics": dataset.metrics or {},
        }

    publish_event(detail_type=STATUS_CHANGED_DETAIL_TYPE, detail=payload)
