"""Unit tests for app.datasets.refresh_service (dataset-ingestion task 6.1).

The Refresh Service re-captures and re-processes an existing dataset as a new
data version from a required capture. These tests cover the branches the design
lists for the Refresh Service unit tests:

- a dataset that is ``requested``/``processing`` → ``already_refreshing`` (no
  version bump, no copy, no transition, no enqueue) — Requirement 6.6;
- an archived dataset → ``restored_and_refreshed`` — Requirement 6.5;
- the capture artifacts (page + plan, and the snapshot when present) are copied
  into ``raw/v{n}`` with keys from ``storage.keys`` — Requirement 6.4;
- a ``dataset_versions`` row is inserted with the caller's trigger, and the
  ``refresh_requested`` transition carries that trigger — Requirement 6.8;
- an upload capture copies the file and mapping.

No real database is used: a tiny in-memory ``FakeSession`` interprets the exact
SQL the module runs (the locked SELECT, the guarded increment, the version-row
INSERT, and the existence probe) so the control flow and the conditional guard
logic are exercised offline. The guard's true concurrency behaviour against
PostgreSQL (two concurrent calls → one version, Property 7) is task 6.3 and runs
under the Compose stack.

``transition``, ``enqueue``, and the S3 copy/exists helpers are patched so the
tests assert *what* the service does without touching AWS.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from app.datasets import refresh_service as rs
from app.datasets.refresh_service import CheckCapture, UploadCapture
from app.db.models import DatasetStatus
from app.storage import keys

_DATASET_ID = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# In-memory fake session mimicking the SQL the module runs
# ---------------------------------------------------------------------------


class FakeDataset:
    """The dataset columns refresh_service reads/writes via raw SQL."""

    def __init__(
        self,
        *,
        status: DatasetStatus,
        data_version: int = 1,
        archived: bool = False,
        exists: bool = True,
    ) -> None:
        self.status = status
        self.data_version = data_version
        self.archived = archived
        self.exists = exists


class _FakeResult:
    def __init__(self, first: Any) -> None:
        self._first = first

    def first(self) -> Any:
        return self._first


class FakeSession:
    """Interprets the four statements _claim_new_version issues.

    Also records the inserted ``dataset_versions`` rows so a test can assert the
    trigger and version that were written.
    """

    def __init__(self, dataset: FakeDataset) -> None:
        self._ds = dataset
        self.inserted_versions: list[dict[str, Any]] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(statement).split())
        bind = _bind_params(statement, params)

        if "FOR UPDATE" in sql:
            if not self._ds.exists:
                return _FakeResult(None)
            return _FakeResult((self._ds.status.value, self._ds.archived))

        if sql.startswith("UPDATE datasets"):
            # Guarded increment. The module only reaches this when the status
            # is not in-flight, but honour the guard anyway for fidelity.
            if self._ds.status.value in ("requested", "processing"):
                return _FakeResult(None)
            self._ds.data_version += 1
            self._ds.archived = False
            # The guarded UPDATE now also flips status to `requested` in the
            # same row-locked write (task 13): this is the real concurrency
            # marker that makes a second racer's guard fail. Reflect it on the
            # fake so the claim's control flow matches the SQL.
            self._ds.status = DatasetStatus.REQUESTED
            return _FakeResult((self._ds.data_version,))

        if sql.startswith("INSERT INTO dataset_versions"):
            self.inserted_versions.append(
                {
                    "dataset_id": bind["id"],
                    "version": bind["version"],
                    "trigger": bind["trigger"],
                }
            )
            return _FakeResult(None)

        if sql.startswith("SELECT 1 FROM datasets"):
            return _FakeResult((1,) if self._ds.exists else None)

        raise AssertionError(f"unexpected statement: {sql}")


def _bind_params(statement: Any, params: dict[str, Any] | None) -> dict[str, Any]:
    """Pull bound parameters out of a TextClause (``.bindparams(...)``).

    Params passed to execute() always win. In SQLAlchemy 2.1.2 a `text(":x")`
    auto-registers placeholder bindparams with value=None, so only fall back to
    a bindparam's own value when it was set via `.bindparams(x=...)` (not None)
    and the name wasn't already supplied via execute()'s params.
    """
    out: dict[str, Any] = dict(params or {})
    compiled = getattr(statement, "_bindparams", {}) or {}
    for name, bp in compiled.items():
        if name not in out and bp.value is not None:
            out[name] = bp.value
    return out


@contextmanager
def _fake_scope(session: FakeSession) -> Any:
    yield session


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def check_capture() -> CheckCapture:
    return CheckCapture(
        page_key=keys.check_page("chk1", "u1"),
        plan_key=keys.check_plan("chk1", "u1"),
        snapshot_key=keys.check_snapshot("chk1", "u1"),
    )


class _Recorder:
    """Captures calls to the patched collaborators."""

    def __init__(self) -> None:
        self.copies: list[tuple[str, str]] = []
        self.existing: set[str] = set()
        self.transitions: list[tuple[str, DatasetStatus, str, dict[str, Any]]] = []
        self.enqueues: list[tuple[str, dict[str, Any], str | None]] = []

    def copy_object(self, src: str, dst: str) -> None:
        self.copies.append((src, dst))

    def object_exists(self, key: str) -> bool:
        return key in self.existing

    def transition(
        self,
        dataset_id: str,
        new_status: DatasetStatus,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.transitions.append((dataset_id, new_status, message, extra or {}))

    def enqueue(
        self,
        queue_url: str,
        body: dict[str, Any],
        *,
        message_group_id: str | None = None,
        message_deduplication_id: str | None = None,
    ) -> None:
        self.enqueues.append((queue_url, body, message_group_id))


@contextmanager
def _patched(session: FakeSession, rec: _Recorder) -> Any:
    with (
        patch.object(rs, "session_scope", lambda: _fake_scope(session)),
        patch.object(rs.s3, "copy_object", rec.copy_object),
        patch.object(rs.s3, "object_exists", rec.object_exists),
        patch.object(rs, "transition", rec.transition),
        patch.object(rs, "enqueue", rec.enqueue),
        patch.object(
            rs,
            "get_settings",
            lambda: type("S", (), {"processing_queue_url": "proc-queue-url"})(),
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# already_refreshing branch (Requirement 6.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [DatasetStatus.REQUESTED, DatasetStatus.PROCESSING])
def test_in_flight_returns_already_refreshing_and_does_nothing(
    status: DatasetStatus, check_capture: CheckCapture
) -> None:
    session = FakeSession(FakeDataset(status=status, data_version=3))
    rec = _Recorder()
    rec.existing.add(check_capture.snapshot_key or "")

    with _patched(session, rec):
        outcome = rs.refresh(_DATASET_ID, "duplicate_submission", check_capture)

    assert outcome == rs.ALREADY_REFRESHING
    # No version bump, no copy, no transition, no enqueue.
    assert session._ds.data_version == 3
    assert session.inserted_versions == []
    assert rec.copies == []
    assert rec.transitions == []
    assert rec.enqueues == []


# ---------------------------------------------------------------------------
# archived branch (Requirement 6.5)
# ---------------------------------------------------------------------------


def test_archived_dataset_is_restored_and_refreshed(check_capture: CheckCapture) -> None:
    session = FakeSession(FakeDataset(status=DatasetStatus.UPDATED, data_version=2, archived=True))
    rec = _Recorder()

    with _patched(session, rec):
        outcome = rs.refresh(_DATASET_ID, "duplicate_submission", check_capture)

    assert outcome == rs.RESTORED_AND_REFRESHED
    assert session._ds.archived is False  # archived_at cleared
    assert session._ds.data_version == 3


def test_idle_dataset_returns_refreshed(check_capture: CheckCapture) -> None:
    session = FakeSession(FakeDataset(status=DatasetStatus.FAILED, data_version=4))
    rec = _Recorder()

    with _patched(session, rec):
        outcome = rs.refresh(_DATASET_ID, "manual_refresh", check_capture)

    assert outcome == rs.REFRESHED
    assert session._ds.data_version == 5


# ---------------------------------------------------------------------------
# capture + plan copied with storage.keys destinations (Requirement 6.4)
# ---------------------------------------------------------------------------


def test_check_capture_copies_page_plan_and_snapshot(check_capture: CheckCapture) -> None:
    session = FakeSession(FakeDataset(status=DatasetStatus.UPDATED, data_version=1))
    rec = _Recorder()
    rec.existing.add(check_capture.snapshot_key or "")  # snapshot present

    with _patched(session, rec):
        rs.refresh(_DATASET_ID, "manual_refresh", check_capture)

    # New version is 2; destinations must come from storage.keys.
    assert (check_capture.page_key, keys.dataset_raw_page(_DATASET_ID, 2, 1)) in rec.copies
    assert (check_capture.plan_key, keys.dataset_raw_plan(_DATASET_ID, 2)) in rec.copies
    assert (
        check_capture.snapshot_key,
        keys.dataset_snapshot(_DATASET_ID, 2),
    ) in rec.copies


def test_check_capture_skips_absent_snapshot(check_capture: CheckCapture) -> None:
    session = FakeSession(FakeDataset(status=DatasetStatus.UPDATED, data_version=1))
    rec = _Recorder()
    # snapshot NOT registered as existing → object_exists returns False

    with _patched(session, rec):
        rs.refresh(_DATASET_ID, "manual_refresh", check_capture)

    snapshot_dsts = [dst for _, dst in rec.copies if "snapshot" in dst]
    assert snapshot_dsts == []
    # page + plan still copied.
    assert (check_capture.page_key, keys.dataset_raw_page(_DATASET_ID, 2, 1)) in rec.copies
    assert (check_capture.plan_key, keys.dataset_raw_plan(_DATASET_ID, 2)) in rec.copies


def test_upload_capture_copies_file_and_mapping() -> None:
    session = FakeSession(FakeDataset(status=DatasetStatus.UPDATED, data_version=7))
    rec = _Recorder()
    capture = UploadCapture(
        file_key=keys.upload_file("up1"),
        mapping_key="uploads/up1/mapping.json",
    )

    with _patched(session, rec):
        rs.refresh(_DATASET_ID, "upload_replace", capture)

    assert (capture.file_key, keys.dataset_raw_upload(_DATASET_ID, 8)) in rec.copies
    assert (capture.mapping_key, keys.dataset_raw_mapping(_DATASET_ID, 8)) in rec.copies


# ---------------------------------------------------------------------------
# dataset_versions row + transition carry the trigger (Requirement 6.8)
# ---------------------------------------------------------------------------


def test_version_row_inserted_with_trigger(check_capture: CheckCapture) -> None:
    session = FakeSession(FakeDataset(status=DatasetStatus.UPDATED, data_version=1))
    rec = _Recorder()

    with _patched(session, rec):
        rs.refresh(_DATASET_ID, "duplicate_submission", check_capture)

    assert session.inserted_versions == [
        {"dataset_id": _DATASET_ID, "version": 2, "trigger": "duplicate_submission"}
    ]


def test_transition_is_refresh_requested_with_trigger(check_capture: CheckCapture) -> None:
    session = FakeSession(FakeDataset(status=DatasetStatus.UPDATED, data_version=1))
    rec = _Recorder()

    with _patched(session, rec):
        rs.refresh(_DATASET_ID, "manual_refresh", check_capture)

    assert len(rec.transitions) == 1
    dataset_id, new_status, message, extra = rec.transitions[0]
    assert dataset_id == _DATASET_ID
    assert new_status is DatasetStatus.REQUESTED
    assert message == "refresh_requested"
    assert extra["trigger"] == "manual_refresh"
    assert extra["version"] == 2


# ---------------------------------------------------------------------------
# enqueue carries IDs only to the FIFO processing queue
# ---------------------------------------------------------------------------


def test_enqueue_processing_message_ids_only(check_capture: CheckCapture) -> None:
    session = FakeSession(FakeDataset(status=DatasetStatus.UPDATED, data_version=1))
    rec = _Recorder()

    with _patched(session, rec):
        rs.refresh(_DATASET_ID, "manual_refresh", check_capture)

    assert len(rec.enqueues) == 1
    queue_url, body, group = rec.enqueues[0]
    assert queue_url == "proc-queue-url"
    assert body == {"dataset_id": _DATASET_ID, "data_version": 2}
    assert group == _DATASET_ID  # FIFO group keyed by dataset


# ---------------------------------------------------------------------------
# ordering: copy before enqueue; nothing enqueued if copy fails
# ---------------------------------------------------------------------------


def test_copy_happens_before_enqueue(check_capture: CheckCapture) -> None:
    session = FakeSession(FakeDataset(status=DatasetStatus.UPDATED, data_version=1))
    order: list[str] = []

    def _copy(src: str, dst: str) -> None:
        order.append("copy")

    def _enqueue(queue_url: str, body: dict[str, Any], **kw: Any) -> None:
        order.append("enqueue")

    with (
        patch.object(rs, "session_scope", lambda: _fake_scope(session)),
        patch.object(rs.s3, "copy_object", _copy),
        patch.object(rs.s3, "object_exists", lambda k: False),
        patch.object(rs, "transition", lambda *a, **k: order.append("transition")),
        patch.object(rs, "enqueue", _enqueue),
        patch.object(
            rs,
            "get_settings",
            lambda: type("S", (), {"processing_queue_url": "q"})(),
        ),
    ):
        rs.refresh(_DATASET_ID, "manual_refresh", check_capture)

    # Copies precede the transition, which precedes the enqueue.
    assert order.index("copy") < order.index("transition") < order.index("enqueue")


# ---------------------------------------------------------------------------
# missing dataset is a loud error, not a silent already_refreshing
# ---------------------------------------------------------------------------


def test_missing_dataset_raises(check_capture: CheckCapture) -> None:
    session = FakeSession(FakeDataset(status=DatasetStatus.UPDATED, exists=False))
    rec = _Recorder()

    with _patched(session, rec), pytest.raises(ValueError, match="does not exist"):
        rs.refresh(_DATASET_ID, "manual_refresh", check_capture)
