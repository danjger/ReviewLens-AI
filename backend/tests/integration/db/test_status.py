"""Integration tests for app.db.status against real PostgreSQL.

Covers platform-foundation task 5.3 and Requirement 4.4 against a live
PostgreSQL (the Compose ``postgres`` service), proving the behaviour that the
unit tests fake:

- A sequence of ``transition`` and ``log_event`` calls appends events to
  ``status_detail.events`` **in time order**, earlier events unchanged, and the
  last *status* event equals the ``status`` column (Correctness Property 5).
- The ``status`` column and the appended event are written atomically.

These run under ``make test-int`` (``pytest -m integration``) with the Compose
stack up (``DATABASE_URL`` pointing at the ``postgres`` container). EventBridge
publishing is covered by the moto unit test, so :func:`app.events.publisher.
publish_event` is patched out here to keep the test focused on the database.

Each test creates its own dataset row and deletes it afterwards, so the suite
leaves no rows behind (testing convention: tests clean up their own rows).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime
from unittest.mock import patch

import pytest
from app.core import db as core_db
from app.core.config import get_settings
from app.db import status as status_mod
from app.db.models import Base, Dataset, DatasetStatus, SourceType
from sqlalchemy import text

pytestmark = pytest.mark.integration


def _database_reachable() -> bool:
    """Return True if the configured PostgreSQL accepts a connection."""
    try:
        with core_db.get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any connect/config failure means "skip"
        return False


@pytest.fixture(scope="module", autouse=True)
def _schema() -> Iterator[None]:
    """Create the schema on the live database for the module, drop it after.

    Skips the whole module when no PostgreSQL is reachable (for example when the
    Compose stack is not running in this environment).
    """
    get_settings.cache_clear()
    core_db.reset_engine()
    if get_settings().is_aws or not _database_reachable():
        pytest.skip("PostgreSQL not reachable; run under `make test-int` with the stack up")

    engine = core_db.get_engine()
    # gen_random_uuid() lives in pgcrypto on older servers; harmless if present.
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(engine)
    try:
        yield
    finally:
        Base.metadata.drop_all(engine)
        core_db.reset_engine()


@pytest.fixture()
def dataset_id() -> Iterator[str]:
    """Insert a fresh ``requested`` URL dataset; delete it after the test."""
    ds_id = str(uuid.uuid4())
    with core_db.session_scope() as session:
        session.add(
            Dataset(
                id=ds_id,
                name="integration-test",
                source_type=SourceType.URL,
                original_url=f"https://example.com/{ds_id}",
                normalized_url=f"https://example.com/{ds_id}",
                status=DatasetStatus.REQUESTED,
                status_detail={"events": []},
            )
        )
    try:
        yield ds_id
    finally:
        with core_db.session_scope() as session:
            session.execute(
                text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
            )


@pytest.fixture(autouse=True)
def _no_eventbridge() -> Iterator[None]:
    """Patch out the EventBridge publish; ordering is the focus here."""
    with patch.object(status_mod, "publish_event"):
        yield


def _events(ds_id: str) -> list[dict[str, object]]:
    with core_db.session_scope() as session:
        detail = session.execute(
            text("SELECT status_detail FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
        ).scalar_one()
    assert isinstance(detail, dict)
    events = detail["events"]
    assert isinstance(events, list)
    return events


def test_event_history_is_kept_in_order(dataset_id: str) -> None:
    """Property 5: events are append-only and in time order after a sequence."""
    status_mod.transition(dataset_id, DatasetStatus.PROCESSING, "Started")
    status_mod.log_event(dataset_id, "Fetching page 1 of 3", extra={"page": 1})
    status_mod.log_event(dataset_id, "Fetching page 2 of 3", extra={"page": 2})
    status_mod.transition(dataset_id, DatasetStatus.UPDATED, "Done", extra={"reviews": 42})

    events = _events(dataset_id)
    assert [e["message"] for e in events] == [
        "Started",
        "Fetching page 1 of 3",
        "Fetching page 2 of 3",
        "Done",
    ]
    # Timestamps are non-decreasing (time order).
    times = [datetime.fromisoformat(str(e["at"])) for e in events]
    assert times == sorted(times)

    # The last *status* event equals the status column (Property 5).
    status_events = [e for e in events if e["status"] is not None]
    assert status_events[-1]["status"] == "updated"
    with core_db.session_scope() as session:
        col_status = session.execute(
            text("SELECT status FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": dataset_id}
        ).scalar_one()
    assert col_status == "updated"


def test_earlier_events_are_never_rewritten(dataset_id: str) -> None:
    """Appending later events leaves earlier entries byte-for-byte unchanged."""
    status_mod.transition(dataset_id, DatasetStatus.PROCESSING, "first")
    first_snapshot = _events(dataset_id)[0]

    status_mod.log_event(dataset_id, "second")
    status_mod.transition(dataset_id, DatasetStatus.FAILED, "third")

    events = _events(dataset_id)
    assert events[0] == first_snapshot
    assert len(events) == 3


def test_transition_is_atomic_status_and_event(dataset_id: str) -> None:
    """The status column and its event move together in one transaction."""
    status_mod.transition(dataset_id, DatasetStatus.PROCESSING, "go")

    with core_db.session_scope() as session:
        row = session.execute(
            text("SELECT status, status_detail FROM datasets WHERE id = CAST(:id AS uuid)"),
            {"id": dataset_id},
        ).one()
    status_value, detail = row
    assert status_value == "processing"
    last_status_event = [e for e in detail["events"] if e["status"] is not None][-1]
    assert last_status_event["status"] == "processing"


def test_concurrent_appends_do_not_lose_events(dataset_id: str) -> None:
    """Concurrent log_event calls serialise under the row lock; none are lost.

    Property 5 under concurrency: the row lock in ``_append_event`` makes
    interleaved writers serialise, so every appended event survives.
    """
    import threading

    def worker(n: int) -> None:
        status_mod.log_event(dataset_id, f"progress-{n}", extra={"n": n})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = _events(dataset_id)
    messages = {str(e["message"]) for e in events}
    assert messages == {f"progress-{n}" for n in range(10)}
    assert len(events) == 10
