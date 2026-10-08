"""Property-based test for app.datasets.refresh_service (dataset-ingestion 6.3).

Property 7: One new version per refresh.
  For any number of concurrent refresh calls on a dataset that is not
  processing, ``data_version`` SHALL increase by exactly one.
  Validates: Requirement 6.6

The *true* concurrency guarantee against PostgreSQL (two threads racing the
``SELECT ... FOR UPDATE`` guard) is proved by the integration test
``tests/integration/datasets/test_refresh_service_int.py``. This property test
covers the complementary logical invariant across a wide input space with
Hypothesis: for **any** starting status and **any** number of ``refresh`` calls
replayed through a shared in-memory model of the dataset row, the version
increases by exactly one when the dataset starts idle, and not at all when it
starts in flight.

The row lock in ``_claim_new_version`` makes concurrent refreshers *serialise*,
so a correct model of that behaviour is a sequence of serial ``refresh`` calls
sharing one persistent row: the first winner bumps the version and transitions
the row to ``requested`` (an in-flight status), and every later call then takes
the ``already_refreshing`` branch. A single stateful ``FakeSession`` (persisting
across calls, unlike the per-call fakes in the task-6.1 unit tests) models that
exactly, so the guard's decision logic is exercised offline. ``transition`` is
modelled so it flips the shared row's status to ``requested`` just as the real
``db.status.transition`` does; ``enqueue`` and the S3 copy are inert.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from app.datasets import refresh_service as rs
from app.datasets.refresh_service import CheckCapture
from app.db.models import DatasetStatus
from app.storage import keys
from hypothesis import given, settings
from hypothesis import strategies as st

_DATASET_ID = "11111111-1111-1111-1111-111111111111"
_IN_FLIGHT = (DatasetStatus.REQUESTED, DatasetStatus.PROCESSING)


class _StatefulRow:
    """A persistent model of the one dataset row the Refresh Service guards."""

    def __init__(self, status: DatasetStatus, data_version: int) -> None:
        self.status = status
        self.data_version = data_version
        self.archived = False
        self.inserted_versions: list[int] = []


class _FakeResult:
    def __init__(self, first: Any) -> None:
        self._first = first

    def first(self) -> Any:
        return self._first


class _StatefulSession:
    """Interprets the SQL ``_claim_new_version`` runs against a shared row.

    Unlike the task-6.1 unit fakes (one fresh row per call), this session shares
    one :class:`_StatefulRow` across every ``refresh`` call so the guard sees
    the status the previous winner left behind — the serialised view the real
    ``SELECT ... FOR UPDATE`` lock produces under concurrency.
    """

    def __init__(self, row: _StatefulRow) -> None:
        self._row = row

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(statement).split())
        bind = _bind(statement, params)

        if "FOR UPDATE" in sql:
            return _FakeResult((self._row.status.value, self._row.archived))

        if sql.startswith("UPDATE datasets"):
            # Guarded increment: only applies when not in flight.
            if self._row.status in _IN_FLIGHT:
                return _FakeResult(None)
            self._row.data_version += 1
            self._row.archived = False
            return _FakeResult((self._row.data_version,))

        if sql.startswith("INSERT INTO dataset_versions"):
            self._row.inserted_versions.append(int(bind["version"]))
            return _FakeResult(None)

        raise AssertionError(f"unexpected statement: {sql}")


def _bind(statement: Any, params: dict[str, Any] | None) -> dict[str, Any]:
    # Params passed to execute() always win. In SQLAlchemy 2.1.2 a `text(":x")`
    # auto-registers placeholder bindparams with value=None, so only fall back
    # to a bindparam's own value when it was set via `.bindparams(x=...)` (not
    # None) and the name wasn't already supplied via execute()'s params.
    out: dict[str, Any] = dict(params or {})
    for name, bp in (getattr(statement, "_bindparams", {}) or {}).items():
        if name not in out and bp.value is not None:
            out[name] = bp.value
    return out


@contextmanager
def _scope(session: _StatefulSession) -> Iterator[_StatefulSession]:
    yield session


def _capture() -> CheckCapture:
    return CheckCapture(
        page_key=keys.check_page("chk", "u1"),
        plan_key=keys.check_plan("chk", "u1"),
        snapshot_key=None,
    )


@settings(max_examples=200)
@given(
    start_status=st.sampled_from(list(DatasetStatus)),
    start_version=st.integers(min_value=0, max_value=50),
    n_calls=st.integers(min_value=1, max_value=8),
)
def test_one_new_version_per_refresh(
    start_status: DatasetStatus,
    start_version: int,
    n_calls: int,
) -> None:
    """Property 7: One new version per refresh.

    For any number of refresh calls on a dataset that is not processing,
    data_version SHALL increase by exactly one.
    Validates: Requirement 6.6
    """
    row = _StatefulRow(status=start_status, data_version=start_version)
    session = _StatefulSession(row)

    # transition() flips the shared row to `requested` like db.status does, so
    # the next serialised caller sees an in-flight status.
    def _transition(dataset_id: str, new_status: DatasetStatus, *a: Any, **k: Any) -> None:
        row.status = new_status

    outcomes: list[str] = []
    with (
        patch.object(rs, "session_scope", lambda: _scope(session)),
        patch.object(rs.s3, "copy_object", lambda src, dst: None),
        patch.object(rs.s3, "object_exists", lambda key: False),
        patch.object(rs, "transition", _transition),
        patch.object(rs, "enqueue", lambda *a, **k: None),
        patch.object(rs, "get_settings", lambda: type("S", (), {"processing_queue_url": "q"})()),
    ):
        for _ in range(n_calls):
            outcomes.append(rs.refresh(_DATASET_ID, "manual_refresh", _capture()))

    started_in_flight = start_status in _IN_FLIGHT
    if started_in_flight:
        # A dataset already refreshing never bumps the version (Requirement 6.6).
        assert row.data_version == start_version
        assert row.inserted_versions == []
        assert set(outcomes) == {rs.ALREADY_REFRESHING}
    else:
        # Exactly one new version regardless of how many calls raced.
        assert row.data_version == start_version + 1
        assert row.inserted_versions == [start_version + 1]
        # Exactly one winner; the rest reported already_refreshing.
        assert outcomes.count(rs.ALREADY_REFRESHING) == n_calls - 1
        assert outcomes.count(rs.ALREADY_REFRESHING) + 1 == n_calls
