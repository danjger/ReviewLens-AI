"""Integration tests for app.datasets.refresh_service (dataset-ingestion 6.3).

Task 6.1's unit tests (``tests/unit/datasets/test_refresh_service.py``) proved
the Refresh Service's control flow against an in-memory fake session; the
module docstring there is explicit that "the guard's true concurrency behaviour
against PostgreSQL (two concurrent calls -> one version, Property 7) is task 6.3
and runs under the Compose stack." This suite is that task: it drives
:func:`app.datasets.refresh_service.refresh` against the **real** backing
services the design calls for —

* the Compose PostgreSQL database (the ``datasets`` / ``dataset_versions``
  tables, the ``WHERE status NOT IN ('requested','processing')`` guard, and the
  ``SELECT ... FOR UPDATE`` row lock);
* the LocalStack S3 bucket (the capture page/plan/snapshot copied into
  ``raw/v{n}`` with keys from :mod:`app.storage.keys`);
* the LocalStack SQS FIFO ``processing-queue.fifo`` (the enqueued processing
  message ``{dataset_id, data_version}`` with ``MessageGroupId = dataset_id``).

What it proves (Requirements 6.4, 6.5, 6.6, 6.8):

1. **``processing`` -> ``already_refreshing``** and **``requested`` ->
   ``already_refreshing``**: no version bump, no row, no copy, no enqueue
   (Requirement 6.6).
2. **archived -> ``restored_and_refreshed``** with ``archived_at`` cleared
   (Requirement 6.5).
3. **An idle dataset -> ``refreshed``**: ``data_version`` increments by one, the
   capture page+plan (and snapshot) are copied into ``raw/v{n}`` /
   ``snapshot/v{n}.png``, a ``dataset_versions`` row is inserted with the right
   trigger, a ``refresh_requested`` event/transition is appended, and a
   processing message is enqueued to the FIFO queue (Requirements 6.4, 6.8).
4. **Two concurrent refresh calls on a non-processing dataset produce exactly
   ONE new version** — Correctness Property 7 / Requirement 6.6 — a real
   race of threads against PostgreSQL's row lock and guarded update.

Running
-------
Run with ``make test-int`` under ``make up`` (needs LocalStack **and** the
Compose PostgreSQL). The module and each test skip cleanly when either is
unavailable, so the suite still *collects* without the stack. Each test uses
its own random dataset id and cleans up its database rows and S3 prefix.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator

import boto3
import httpx
import pytest
from app.core import db as core_db
from app.core.config import get_settings
from app.datasets import refresh_service as rs
from app.datasets.refresh_service import CheckCapture
from app.db.models import Base, Dataset, DatasetStatus, SourceType
from app.storage import keys, s3
from sqlalchemy import text

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"
#: The FIFO processing queue provisioned by ``infra/localstack-init``.
_PROCESSING_QUEUE_URL = (
    "http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/processing-queue.fifo"
)


# ---------------------------------------------------------------------------
# Availability probes (skip cleanly without the stack)
# ---------------------------------------------------------------------------


def _localstack_up() -> bool:
    try:
        resp = httpx.get(f"{_ENDPOINT}/_localstack/health", timeout=2.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _database_reachable() -> bool:
    try:
        with core_db.get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any failure means skip
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point AWS clients at LocalStack and reset memoised handles."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", _ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", _S3_BUCKET)
    monkeypatch.setenv("EVENTBRIDGE_BUS_NAME", "reviewlens-events")
    monkeypatch.setenv("PROCESSING_QUEUE_URL", _PROCESSING_QUEUE_URL)
    get_settings.cache_clear()
    s3.reset_client()
    from app.core import queue
    from app.events import publisher

    queue.reset_client()
    publisher.reset_client()
    try:
        yield
    finally:
        get_settings.cache_clear()
        s3.reset_client()
        queue.reset_client()
        publisher.reset_client()


@pytest.fixture(scope="module", autouse=True)
def _require_stack() -> None:
    if not _localstack_up():
        pytest.skip("LocalStack not reachable; run under `make test-int`")


@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    """Ensure the DB schema exists; skip when no PostgreSQL is reachable."""
    get_settings.cache_clear()
    core_db.reset_engine()
    if get_settings().is_aws or not _database_reachable():
        pytest.skip("PostgreSQL not reachable; run under `make test-int` with the stack up")
    engine = core_db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(engine)
    yield
    core_db.reset_engine()


@pytest.fixture()
def dataset_factory() -> Iterator[object]:
    """Create datasets with a known status/version; clean up rows + S3 after."""
    created: list[str] = []

    def _make(
        *,
        status: DatasetStatus,
        data_version: int = 1,
        archived: bool = False,
    ) -> str:
        ds_id = str(uuid.uuid4())
        with core_db.session_scope() as session:
            session.add(
                Dataset(
                    id=ds_id,
                    name=f"refresh-int-{ds_id[:8]}",
                    source_type=SourceType.URL,
                    original_url=f"https://example.com/{ds_id}",
                    normalized_url=f"https://example.com/{ds_id}",
                    status=status,
                    status_detail={"events": []},
                    data_version=data_version,
                    archived_at=(text("now()") if archived else None),  # type: ignore[arg-type]
                )
            )
            if archived:
                session.execute(
                    text("UPDATE datasets SET archived_at = now() WHERE id = CAST(:id AS uuid)"),
                    {"id": ds_id},
                )
            # Seed the matching version rows so the FK + PK (dataset_id, version)
            # never collide when the Refresh Service inserts v{n+1}.
            for v in range(1, data_version + 1):
                session.execute(
                    text(
                        "INSERT INTO dataset_versions (dataset_id, version, trigger, requested_at) "
                        "VALUES (CAST(:id AS uuid), :v, 'initial', now())"
                    ),
                    {"id": ds_id, "v": v},
                )
        created.append(ds_id)
        return ds_id

    try:
        yield _make
    finally:
        client = s3._get_s3_client()
        with core_db.session_scope() as session:
            for ds_id in created:
                session.execute(
                    text("DELETE FROM dataset_versions WHERE dataset_id = CAST(:id AS uuid)"),
                    {"id": ds_id},
                )
                session.execute(
                    text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
                )
        for ds_id in created:
            listed = client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=keys.dataset_prefix(ds_id))
            for obj in listed.get("Contents", []):
                key = obj.get("Key")
                if key:
                    client.delete_object(Bucket=_S3_BUCKET, Key=key)


@pytest.fixture()
def capture_factory() -> Iterator[object]:
    """Stage a Check capture (page+plan, optional snapshot) in S3; clean up after."""
    prefixes: list[str] = []

    def _make(*, with_snapshot: bool = True) -> CheckCapture:
        check_id = f"it-{uuid.uuid4().hex}"
        item_id = "u1"
        page_key = keys.check_page(check_id, item_id)
        plan_key = keys.check_plan(check_id, item_id)
        snapshot_key = keys.check_snapshot(check_id, item_id)

        s3.put_bytes(
            page_key,
            b"<html><body><main>captured reviews</main></body></html>",
            content_type="text/html; charset=utf-8",
        )
        s3.put_bytes(
            plan_key,
            json.dumps({"method": "selectors", "degraded": False}).encode("utf-8"),
            content_type="application/json",
        )
        if with_snapshot:
            s3.put_bytes(snapshot_key, b"\x89PNG snapshot bytes", content_type="image/png")

        prefixes.append(keys.check_prefix(check_id, item_id))
        return CheckCapture(
            page_key=page_key,
            plan_key=plan_key,
            snapshot_key=snapshot_key if with_snapshot else None,
        )

    try:
        yield _make
    finally:
        client = s3._get_s3_client()
        for prefix in prefixes:
            listed = client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=prefix)
            for obj in listed.get("Contents", []):
                key = obj.get("Key")
                if key:
                    client.delete_object(Bucket=_S3_BUCKET, Key=key)


# ---------------------------------------------------------------------------
# Helpers reading state back from the real services
# ---------------------------------------------------------------------------


def _dataset_row(ds_id: str) -> tuple[str, int, bool]:
    """Return ``(status, data_version, is_archived)`` from PostgreSQL."""
    with core_db.session_scope() as session:
        row = session.execute(
            text(
                "SELECT status, data_version, (archived_at IS NOT NULL) "
                "FROM datasets WHERE id = CAST(:id AS uuid)"
            ),
            {"id": ds_id},
        ).one()
    return str(row[0]), int(row[1]), bool(row[2])


def _version_rows(ds_id: str) -> list[tuple[int, str]]:
    """Return ``(version, trigger)`` rows for a dataset, ordered by version."""
    with core_db.session_scope() as session:
        rows = session.execute(
            text(
                "SELECT version, trigger FROM dataset_versions "
                "WHERE dataset_id = CAST(:id AS uuid) ORDER BY version"
            ),
            {"id": ds_id},
        ).all()
    return [(int(v), str(t)) for v, t in rows]


def _status_events(ds_id: str) -> list[dict[str, object]]:
    with core_db.session_scope() as session:
        detail = session.execute(
            text("SELECT status_detail FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
        ).scalar_one()
    assert isinstance(detail, dict)
    events = detail.get("events", [])
    assert isinstance(events, list)
    return events


def _object_exists(key: str) -> bool:
    return s3.object_exists(key)


def _drain_processing_queue() -> list[dict[str, object]]:
    """Receive and delete all messages on the FIFO processing queue, as dicts."""
    sqs = boto3.client("sqs", region_name=_REGION, endpoint_url=_ENDPOINT)
    bodies: list[dict[str, object]] = []
    while True:
        resp = sqs.receive_message(
            QueueUrl=_PROCESSING_QUEUE_URL,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
            VisibilityTimeout=1,
        )
        messages = resp.get("Messages", [])
        if not messages:
            break
        for msg in messages:
            bodies.append(json.loads(msg["Body"]))
            sqs.delete_message(QueueUrl=_PROCESSING_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
    return bodies


@pytest.fixture()
def _clean_queue() -> Iterator[None]:
    """Purge the processing queue before and after so each test sees only its own."""
    _drain_processing_queue()
    yield
    _drain_processing_queue()


# ---------------------------------------------------------------------------
# already_refreshing branch (Requirement 6.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [DatasetStatus.REQUESTED, DatasetStatus.PROCESSING])
def test_in_flight_dataset_is_already_refreshing(
    status: DatasetStatus,
    dataset_factory: object,
    capture_factory: object,
    _clean_queue: None,
) -> None:
    """A `requested`/`processing` dataset refreshes nothing (Requirement 6.6)."""
    ds_id = dataset_factory(status=status, data_version=3)  # type: ignore[operator]
    capture = capture_factory()  # type: ignore[operator]

    outcome = rs.refresh(ds_id, "manual_refresh", capture)

    assert outcome == rs.ALREADY_REFRESHING
    db_status, version, _ = _dataset_row(ds_id)
    assert db_status == status.value
    assert version == 3  # no bump
    # Only the seeded initial rows exist; no new version row was inserted.
    assert _version_rows(ds_id) == [(1, "initial"), (2, "initial"), (3, "initial")]
    # Nothing copied for the new version, nothing enqueued.
    assert not _object_exists(keys.dataset_raw_page(ds_id, 4, 1))
    assert _drain_processing_queue() == []


# ---------------------------------------------------------------------------
# archived branch (Requirement 6.5)
# ---------------------------------------------------------------------------


def test_archived_dataset_is_restored_and_refreshed(
    dataset_factory: object,
    capture_factory: object,
    _clean_queue: None,
) -> None:
    """Archived -> restored (archived_at cleared) + new version (Requirement 6.5)."""
    ds_id = dataset_factory(status=DatasetStatus.UPDATED, data_version=2, archived=True)  # type: ignore[operator]
    capture = capture_factory()  # type: ignore[operator]

    outcome = rs.refresh(ds_id, "duplicate_submission", capture)

    assert outcome == rs.RESTORED_AND_REFRESHED
    db_status, version, is_archived = _dataset_row(ds_id)
    assert is_archived is False  # archived_at cleared on restore
    assert version == 3
    assert db_status == DatasetStatus.REQUESTED.value
    assert (3, "duplicate_submission") in _version_rows(ds_id)


# ---------------------------------------------------------------------------
# idle dataset: full happy path (Requirements 6.4, 6.8)
# ---------------------------------------------------------------------------


def test_idle_dataset_refresh_copies_inserts_transitions_and_enqueues(
    dataset_factory: object,
    capture_factory: object,
    _clean_queue: None,
) -> None:
    """Idle -> refreshed: version bump, copy, row, transition, enqueue (6.4, 6.8)."""
    ds_id = dataset_factory(status=DatasetStatus.UPDATED, data_version=1)  # type: ignore[operator]
    capture = capture_factory(with_snapshot=True)  # type: ignore[operator]

    outcome = rs.refresh(ds_id, "manual_refresh", capture)

    assert outcome == rs.REFRESHED

    # data_version incremented by one and the dataset is back to `requested`.
    db_status, version, _ = _dataset_row(ds_id)
    assert version == 2
    assert db_status == DatasetStatus.REQUESTED.value

    # The capture page + plan (and snapshot) were copied into raw/v2 using the
    # storage.keys destinations (Requirement 6.4).
    assert _object_exists(keys.dataset_raw_page(ds_id, 2, 1))
    assert _object_exists(keys.dataset_raw_plan(ds_id, 2))
    assert _object_exists(keys.dataset_snapshot(ds_id, 2))

    # A dataset_versions row was inserted with the caller's trigger.
    assert (2, "manual_refresh") in _version_rows(ds_id)

    # A refresh_requested event was appended through db.status (Requirement 6.8).
    events = _status_events(ds_id)
    assert any(e.get("message") == "refresh_requested" for e in events)
    last = events[-1]
    assert last.get("status") == "requested"

    # A processing message carrying IDs only was enqueued to the FIFO queue.
    bodies = _drain_processing_queue()
    assert {"dataset_id": ds_id, "data_version": 2} in bodies


def test_refresh_without_snapshot_copies_page_and_plan_only(
    dataset_factory: object,
    capture_factory: object,
    _clean_queue: None,
) -> None:
    """A capture with no snapshot copies page + plan, never a snapshot object."""
    ds_id = dataset_factory(status=DatasetStatus.FAILED, data_version=1)  # type: ignore[operator]
    capture = capture_factory(with_snapshot=False)  # type: ignore[operator]

    rs.refresh(ds_id, "manual_refresh", capture)

    assert _object_exists(keys.dataset_raw_page(ds_id, 2, 1))
    assert _object_exists(keys.dataset_raw_plan(ds_id, 2))
    assert not _object_exists(keys.dataset_snapshot(ds_id, 2))


# ---------------------------------------------------------------------------
# Correctness Property 7: one new version per refresh under real concurrency
# ---------------------------------------------------------------------------


def test_two_concurrent_refreshes_produce_exactly_one_version(
    dataset_factory: object,
    capture_factory: object,
    _clean_queue: None,
) -> None:
    """Property 7: concurrent refresh calls increase data_version by exactly one.

    Two threads call ``refresh`` on the same idle dataset at the same time. The
    ``SELECT ... FOR UPDATE`` lock plus the ``WHERE status NOT IN
    ('requested','processing')`` guard mean exactly one thread wins (it bumps
    the version and transitions the dataset to ``requested``); the other arrives
    after that commit, sees an in-flight status, and reports
    ``already_refreshing`` without creating a second version.

    _Validates: Requirement 6.6 (design Correctness Property 7)._
    """
    ds_id = dataset_factory(status=DatasetStatus.UPDATED, data_version=1)  # type: ignore[operator]
    # Each racer gets its own staged capture so the copies never collide.
    captures = [capture_factory(), capture_factory()]  # type: ignore[operator]

    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _worker(idx: int) -> None:
        try:
            barrier.wait(timeout=10)  # release both threads together
            results.append(rs.refresh(ds_id, "manual_refresh", captures[idx]))
        except BaseException as exc:  # noqa: BLE001 - carried to the asserting thread
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"a refresh thread raised: {errors}"

    # Exactly one new version: data_version went 1 -> 2, never 3.
    _, version, _ = _dataset_row(ds_id)
    assert version == 2, f"expected exactly one new version, got data_version={version}"

    # Exactly one new dataset_versions row (plus the seeded v1).
    versions = _version_rows(ds_id)
    assert versions == [(1, "initial"), (2, "manual_refresh")], versions

    # Exactly one winner and one `already_refreshing`.
    assert sorted(results) == sorted([rs.REFRESHED, rs.ALREADY_REFRESHING]), results

    # Exactly one processing message was enqueued for the single new version.
    bodies = _drain_processing_queue()
    assert bodies == [{"dataset_id": ds_id, "data_version": 2}], bodies


def test_many_concurrent_refreshes_produce_exactly_one_version(
    dataset_factory: object,
    capture_factory: object,
    _clean_queue: None,
) -> None:
    """Property 7 at higher contention: N racers still yield exactly one version."""
    n = 6
    ds_id = dataset_factory(status=DatasetStatus.UPDATED, data_version=1)  # type: ignore[operator]
    captures = [capture_factory() for _ in range(n)]  # type: ignore[operator]

    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(n)

    def _worker(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            results.append(rs.refresh(ds_id, "manual_refresh", captures[idx]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"a refresh thread raised: {errors}"

    _, version, _ = _dataset_row(ds_id)
    assert version == 2, f"expected exactly one new version, got data_version={version}"
    assert _version_rows(ds_id) == [(1, "initial"), (2, "manual_refresh")]
    # Exactly one winner; the rest all report already_refreshing.
    assert results.count(rs.REFRESHED) == 1
    assert results.count(rs.ALREADY_REFRESHING) == n - 1
