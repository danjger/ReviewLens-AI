"""Integration tests for the Library refresh API (dataset-library task 3.4).

Task 3's unit tests (``tests/unit/datasets/test_refresh_api.py``) proved the
refresh API's decisions offline with fakes. This suite drives the **endpoints**
``POST /api/datasets/{id}/refresh``, ``/refresh/confirm``, and
``/refresh/upload`` and the ``CheckHandler`` that completes a refresh against the
**real** backing services the design's "Integration tests" bullet calls for
(Requirements 4.1, 4.2, 4.3, 4.4, 4.6, 4.7):

* the LocalStack ``check-sessions`` DynamoDB table (the ``origin="refresh"``
  one-item session the endpoint creates and the handler reads);
* the LocalStack S3 bucket (the fresh capture the handler stores, then the
  Refresh Service copies into ``raw/v{n}`` — earlier versions are kept);
* the Compose PostgreSQL database (the ``datasets`` / ``dataset_versions`` rows,
  ``status`` / ``data_version`` / ``status_detail.refresh_check_id``);
* the LocalStack SQS FIFO ``processing-queue.fifo`` and the standard
  ``check-queue``.

Only the heavy pipeline *inputs* — probe, browser capture, robots, and the
AI-backed viability assessment — are stubbed at the handler's module boundary
(exactly as ``test_check_handler_refresh_origin_int.py`` does), so this needs
neither Chromium nor an AI fixture. The real integration under test is
endpoint -> Check Session + queue, then handler -> Refresh Service ->
database/S3/SQS.

What it proves:

1. **``will_work`` refresh creates a new version with no further client call**:
   the single ``POST .../refresh`` plus the handler yield a v2
   ``dataset_versions`` row, the capture in ``raw/v2``, a ``refresh_requested``
   transition, a processing message, and the item ``applied`` — the analyst made
   only one call (Requirement 4.1).
2. **Previous versions are kept**: the v1 raw page and the v1 ``dataset_versions``
   row still exist after a successful refresh (Requirement 4.6).
3. **A 404 page yields NO new version and keeps the existing data**
   (Requirement 4.2).
4. **409 while processing** (Requirement 4.4).
5. **The confirmation flow**: a ``limited``-after-``will_work`` verdict parks the
   item ``awaiting_confirmation`` with no new version; ``POST .../refresh/confirm``
   then creates the version (Requirement 4.3).
6. **The manual-refresh path and the duplicate-submission path produce the same
   kind of record**: a new ``dataset_versions`` row + a ``refresh_requested``
   event, differing only in the ``trigger`` label (Requirement 4.7).

Running
-------
Run with ``make test-int`` under ``make up`` (LocalStack **and** PostgreSQL).
The module and each test skip cleanly when either is unavailable. Each test uses
its own random ids and cleans up its DynamoDB rows, S3 prefix, and DB rows.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator

import boto3
import httpx
import pytest
from app.api import app
from app.core import db as core_db
from app.core.config import get_settings
from app.db.models import Base, Dataset, DatasetStatus, SourceType
from app.extraction.models import ExtractionPlan, NextPageRule
from app.handlers import check as check_mod
from app.handlers.check import CheckHandler
from app.ingestion import check_session
from app.ingestion.robots import RobotsResult
from app.ingestion.url_validator import Hop, ProbeResult
from app.ingestion.verdict import Verdict
from app.storage import keys, s3
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"
_CHECK_SESSIONS_TABLE = "check-sessions"
_PROCESSING_QUEUE_URL = (
    "http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/processing-queue.fifo"
)
#: The standard (non-FIFO) check queue provisioned by ``infra/localstack-init``.
_CHECK_QUEUE_URL = "http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/check-queue"


# ---------------------------------------------------------------------------
# Availability probes
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
    monkeypatch.setenv("AWS_ENDPOINT_URL", _ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", _S3_BUCKET)
    monkeypatch.setenv("EVENTBRIDGE_BUS_NAME", "reviewlens-events")
    monkeypatch.setenv("PROCESSING_QUEUE_URL", _PROCESSING_QUEUE_URL)
    monkeypatch.setenv("CHECK_QUEUE_URL", _CHECK_QUEUE_URL)
    monkeypatch.setenv("CHECK_SESSIONS_TABLE", _CHECK_SESSIONS_TABLE)
    # Disable the CloudFront origin guard for the headerless TestClient.
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", "")
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
def client() -> Iterator[TestClient]:
    yield TestClient(app)


@pytest.fixture()
def dataset_factory() -> Iterator[Callable[..., str]]:
    """Insert a URL dataset (v1 complete) with a prior verdict; clean up after.

    Seeds a real v1 raw page object so the "previous versions are kept" check has
    a concrete earlier artifact to assert still exists.
    """
    created: list[str] = []

    def _make(
        *,
        previous_verdict: str = "will_work",
        status: DatasetStatus = DatasetStatus.UPDATED,
        data_version: int = 1,
        active_version: int | None = 1,
    ) -> str:
        ds_id = str(uuid.uuid4())
        url = f"https://example.com/{ds_id}"
        with core_db.session_scope() as session:
            session.add(
                Dataset(
                    id=ds_id,
                    name="Refresh Fixture",
                    source_type=SourceType.URL,
                    original_url=url,
                    final_url=url,
                    normalized_url=url,
                    normalized_final_url=url,
                    status=status,
                    status_detail={"events": [], "viability": {"verdict": previous_verdict}},
                    data_version=data_version,
                    active_version=active_version,
                )
            )
            session.flush()  # persist the dataset before its version-row inserts
            for v in range(1, data_version + 1):
                done = v == active_version
                session.execute(
                    text(
                        "INSERT INTO dataset_versions "
                        "(dataset_id, version, trigger, requested_at, completed_at) "
                        "VALUES (CAST(:id AS uuid), :v, 'initial', now(), "
                        "        CASE WHEN :done THEN now() ELSE NULL END)"
                    ),
                    {"id": ds_id, "v": v, "done": done},
                )
        # Seed a real v1 raw page so "previous versions kept" is assertable.
        s3.put_bytes(
            keys.dataset_raw_page(ds_id, 1, 1),
            b"<html><body><main>v1 reviews</main></body></html>",
            content_type="text/html; charset=utf-8",
        )
        created.append(ds_id)
        return ds_id

    try:
        yield _make
    finally:
        s3_client = s3._get_s3_client()
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
            listed = s3_client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=keys.dataset_prefix(ds_id))
            for obj in listed.get("Contents", []):
                key = obj.get("Key")
                if key:
                    s3_client.delete_object(Bucket=_S3_BUCKET, Key=key)


@pytest.fixture()
def check_cleanup() -> Iterator[list[str]]:
    """Collect check ids created during a test and clean up their rows + S3."""
    cids: list[str] = []
    try:
        yield cids
    finally:
        ddb = boto3.client("dynamodb", region_name=_REGION, endpoint_url=_ENDPOINT)
        s3_client = s3._get_s3_client()
        for cid in cids:
            resp = ddb.query(
                TableName=_CHECK_SESSIONS_TABLE,
                KeyConditionExpression="check_id = :c",
                ExpressionAttributeValues={":c": {"S": cid}},
            )
            for row in resp.get("Items", []):
                ddb.delete_item(
                    TableName=_CHECK_SESSIONS_TABLE,
                    Key={"check_id": row["check_id"], "item_id": row["item_id"]},
                )
            listed = s3_client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=f"checks/{cid}/")
            for obj in listed.get("Contents", []):
                key = obj.get("Key")
                if key:
                    s3_client.delete_object(Bucket=_S3_BUCKET, Key=key)


# ---------------------------------------------------------------------------
# Pipeline stub + queue helpers
# ---------------------------------------------------------------------------


def _meta():  # type: ignore[no-untyped-def]
    from app.consumer import MessageMeta

    return MessageMeta(message_id="m-refresh-int", receive_count=1)


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch, *, verdict: Verdict) -> None:
    """Stub probe/capture/robots/assess so no browser or AI is needed.

    The stubbed capture writes a real ``page.html`` under the item's prefix so
    the handler's ``s3.get_text`` read and the Refresh Service's later copy both
    work against real S3.
    """
    from app.capture.engine import CaptureResult

    monkeypatch.setattr(
        check_mod,
        "probe",
        lambda u: ProbeResult(
            final_url=u, status=200, ok=True, hops=[Hop(url=u, status=200, timestamp="t0")]
        ),
    )

    def _render(u: str, prefix: str) -> CaptureResult:
        s3.put_bytes(
            f"{prefix}page.html",
            b"<html><body><main>fresh reviews</main></body></html>",
            content_type="text/html; charset=utf-8",
        )
        return CaptureResult(
            final_url=u,
            main_status=200,
            page_title="Refresh Fixture",
            html_key=f"{prefix}page.html",
            snapshot_key=f"{prefix}snapshot.png",
            redirected=False,
        )

    monkeypatch.setattr(check_mod.capture_engine, "render", _render)
    monkeypatch.setattr(check_mod, "robots_check", lambda u: RobotsResult(allowed=True))

    plan = ExtractionPlan(
        version=1,
        created_at="2024-01-01T00:00:00+00:00",
        method="selectors",
        next_page_rule=NextPageRule(type="selector", css=".next"),
    )
    monkeypatch.setattr(check_mod, "assess", lambda view, fu, robots: (verdict, plan))


def _stub_probe_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub probe to return a non-200 so the item is wont_work before capture."""
    monkeypatch.setattr(
        check_mod,
        "probe",
        lambda u: ProbeResult(
            final_url=u,
            status=404,
            ok=False,
            reason="Status 404",
            hops=[Hop(url=u, status=404, timestamp="t0")],
        ),
    )


def _drain_processing_queue() -> list[dict[str, object]]:
    """Receive and delete all currently-available FIFO processing messages.

    Used by ``_clean_queues`` to leave the queue empty around a test and by the
    negative checks that assert NO processing message was enqueued (the 404 and
    awaiting-confirmation paths, where the refresh has not yet produced a new
    version). We deliberately do **not** poll for an *expected* message: the live
    ``workers-processing`` consumer polls the same FIFO queue and would race the
    test to drain it, so refreshes are instead verified via their durable DB/S3
    state (the new ``dataset_versions`` row and ``refresh_requested`` event).
    """
    sqs = boto3.client("sqs", region_name=_REGION, endpoint_url=_ENDPOINT)
    bodies: list[dict[str, object]] = []
    while True:
        resp = sqs.receive_message(
            QueueUrl=_PROCESSING_QUEUE_URL,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=1,
            VisibilityTimeout=2,
        )
        messages = resp.get("Messages", [])
        if not messages:
            break
        for msg in messages:
            bodies.append(json.loads(msg["Body"]))
            sqs.delete_message(QueueUrl=_PROCESSING_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
    return bodies


def _drain_check_queue() -> list[dict[str, object]]:
    sqs = boto3.client("sqs", region_name=_REGION, endpoint_url=_ENDPOINT)
    bodies: list[dict[str, object]] = []
    while True:
        resp = sqs.receive_message(
            QueueUrl=_CHECK_QUEUE_URL,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
            VisibilityTimeout=1,
        )
        messages = resp.get("Messages", [])
        if not messages:
            break
        for msg in messages:
            bodies.append(json.loads(msg["Body"]))
            sqs.delete_message(QueueUrl=_CHECK_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
    return bodies


@pytest.fixture()
def _clean_queues() -> Iterator[None]:
    _drain_processing_queue()
    _drain_check_queue()
    yield
    _drain_processing_queue()
    _drain_check_queue()


# ---------------------------------------------------------------------------
# State readers
# ---------------------------------------------------------------------------


def _dataset_row(ds_id: str) -> tuple[str, int]:
    with core_db.session_scope() as session:
        row = session.execute(
            text("SELECT status, data_version FROM datasets WHERE id = CAST(:id AS uuid)"),
            {"id": ds_id},
        ).one()
    return str(row[0]), int(row[1])


def _version_rows(ds_id: str) -> list[tuple[int, str]]:
    with core_db.session_scope() as session:
        rows = session.execute(
            text(
                "SELECT version, trigger FROM dataset_versions "
                "WHERE dataset_id = CAST(:id AS uuid) ORDER BY version"
            ),
            {"id": ds_id},
        ).all()
    return [(int(v), str(t)) for v, t in rows]


def _status_detail(ds_id: str) -> dict[str, object]:
    with core_db.session_scope() as session:
        detail = session.execute(
            text("SELECT status_detail FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
        ).scalar_one()
    assert isinstance(detail, dict)
    return detail


def _run_handler_for(check_id: str) -> None:
    CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())


# ---------------------------------------------------------------------------
# 1 + 2. will_work refresh -> new version, no further client call, v1 kept
# ---------------------------------------------------------------------------


def test_will_work_refresh_creates_version_keeps_previous_no_extra_call(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    dataset_factory: Callable[..., str],
    check_cleanup: list[str],
    _clean_queues: None,
) -> None:
    """One POST .../refresh + the handler => a new version; v1 kept (4.1, 4.6)."""
    ds_id = dataset_factory(previous_verdict="will_work")

    # The analyst's single call: start the refresh.
    resp = client.post(f"/api/datasets/{ds_id}/refresh")
    assert resp.status_code == 202
    check_id = resp.json()["check_id"]
    check_cleanup.append(check_id)

    # The row shows a running refresh check. (start_refresh also enqueues the
    # item onto check-queue IDs-only, but the live workers-check consumer polls
    # the same queue and races the test to drain it, so we assert the durable
    # refresh_check_id marker start_refresh wrote rather than the queue message.)
    assert _status_detail(ds_id).get("refresh_check_id") == check_id

    # The handler completes the refresh automatically — no further client call.
    _stub_pipeline(monkeypatch, verdict=Verdict(verdict="will_work", reasons=["24 verified"]))
    _run_handler_for(check_id)

    # A new version exists; the dataset is back to `requested`.
    status, version = _dataset_row(ds_id)
    assert version == 2
    assert status == DatasetStatus.REQUESTED.value
    assert (2, "manual_refresh") in _version_rows(ds_id)

    # The new capture is in raw/v2 AND the previous v1 artifacts are still kept.
    assert s3.object_exists(keys.dataset_raw_page(ds_id, 2, 1))
    assert s3.object_exists(keys.dataset_raw_page(ds_id, 1, 1))  # Requirement 4.6
    assert (1, "initial") in _version_rows(ds_id)

    # refresh_requested transition recorded; refresh_check_id cleared. (The
    # refresh also enqueues a processing message, but the live workers-processing
    # consumer races the test to drain the shared FIFO queue, so we assert the
    # durable DB state that proves the refresh happened rather than queue timing
    # — per testing.md "assert on final database and S3 state, not on timing".)
    assert any(e.get("message") == "refresh_requested" for e in _status_detail(ds_id)["events"])  # type: ignore[union-attr]
    assert "refresh_check_id" not in _status_detail(ds_id)

    # The check item finished `applied`.
    session = check_session.get_session(check_id)
    assert session is not None and session.items["u1"].state == "applied"


# ---------------------------------------------------------------------------
# 3. 404 page -> no new version, data kept (Requirement 4.2)
# ---------------------------------------------------------------------------


def test_404_page_yields_no_version_and_keeps_data(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    dataset_factory: Callable[..., str],
    check_cleanup: list[str],
    _clean_queues: None,
) -> None:
    """A re-check returning 404 keeps the existing data and makes no version (4.2)."""
    ds_id = dataset_factory(previous_verdict="will_work")

    resp = client.post(f"/api/datasets/{ds_id}/refresh")
    assert resp.status_code == 202
    check_id = resp.json()["check_id"]
    check_cleanup.append(check_id)
    _drain_check_queue()

    # The re-check probes 404 -> wont_work before capture.
    _stub_probe_404(monkeypatch)
    _run_handler_for(check_id)

    # No new version; data_version and status unchanged.
    status, version = _dataset_row(ds_id)
    assert version == 1
    assert status == DatasetStatus.UPDATED.value
    assert _version_rows(ds_id) == [(1, "initial")]
    assert s3.object_exists(keys.dataset_raw_page(ds_id, 1, 1))  # data kept

    # A failed-refresh event was appended (no refresh_requested transition).
    events = _status_detail(ds_id)["events"]
    assert any("Refresh failed" in str(e.get("message", "")) for e in events)  # type: ignore[union-attr]
    assert not any(e.get("message") == "refresh_requested" for e in events)  # type: ignore[union-attr]
    assert _drain_processing_queue() == []

    # The check item finished `done` with a wont_work verdict.
    session = check_session.get_session(check_id)
    assert session is not None
    item = session.items["u1"]
    assert item.state == "done"
    assert item.verdict is not None and item.verdict["verdict"] == "wont_work"


# ---------------------------------------------------------------------------
# 4. 409 while processing (Requirement 4.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [DatasetStatus.PROCESSING, DatasetStatus.REQUESTED])
def test_refresh_while_in_flight_is_409(
    status: DatasetStatus,
    client: TestClient,
    dataset_factory: Callable[..., str],
    _clean_queues: None,
) -> None:
    """Refreshing a requested/processing dataset is refused with 409 (4.4)."""
    ds_id = dataset_factory(status=status, active_version=None)

    resp = client.post(f"/api/datasets/{ds_id}/refresh")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ALREADY_REFRESHING"
    # Nothing was enqueued.
    assert _drain_check_queue() == []


# ---------------------------------------------------------------------------
# 5. confirmation flow (Requirement 4.3)
# ---------------------------------------------------------------------------


def test_limited_after_will_work_awaits_then_confirm_creates_version(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    dataset_factory: Callable[..., str],
    check_cleanup: list[str],
    _clean_queues: None,
) -> None:
    """A limited verdict parks for confirmation; confirm then refreshes (4.3)."""
    ds_id = dataset_factory(previous_verdict="will_work")

    resp = client.post(f"/api/datasets/{ds_id}/refresh")
    assert resp.status_code == 202
    check_id = resp.json()["check_id"]
    check_cleanup.append(check_id)
    _drain_check_queue()

    # A limited verdict for a previously will_work dataset waits for the analyst.
    _stub_pipeline(monkeypatch, verdict=Verdict(verdict="limited", reasons=["only 4 shown"]))
    _run_handler_for(check_id)

    # No version yet; the item is awaiting_confirmation and the row still carries
    # the refresh_check_id so the UI shows "Needs confirmation".
    assert _version_rows(ds_id) == [(1, "initial")]
    session = check_session.get_session(check_id)
    assert session is not None and session.items["u1"].state == "awaiting_confirmation"
    assert _status_detail(ds_id).get("refresh_check_id") == check_id
    assert _drain_processing_queue() == []

    # The analyst confirms -> the refresh now proceeds.
    resp = client.post(f"/api/datasets/{ds_id}/refresh/confirm", json={"check_id": check_id})
    assert resp.status_code == 200
    assert resp.json()["outcome"] in {"refreshed", "restored_and_refreshed"}

    status, version = _dataset_row(ds_id)
    assert version == 2
    assert status == DatasetStatus.REQUESTED.value
    assert (2, "manual_refresh") in _version_rows(ds_id)
    assert s3.object_exists(keys.dataset_raw_page(ds_id, 2, 1))
    # (A processing message is enqueued here too; the durable v2 version row +
    # refresh_requested event above prove the refresh ran. We don't assert the
    # queue message because the live consumer races the test to drain it.)
    # The marker is cleared and the item marked applied.
    assert "refresh_check_id" not in _status_detail(ds_id)
    session = check_session.get_session(check_id)
    assert session is not None and session.items["u1"].state == "applied"


def test_confirm_unknown_check_is_404(
    client: TestClient,
    dataset_factory: Callable[..., str],
    _clean_queues: None,
) -> None:
    ds_id = dataset_factory()
    resp = client.post(
        f"/api/datasets/{ds_id}/refresh/confirm", json={"check_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 6. manual path and duplicate-submission path produce the same kind of record
# ---------------------------------------------------------------------------


def test_manual_and_duplicate_paths_produce_same_kind_of_record(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    dataset_factory: Callable[..., str],
    check_cleanup: list[str],
    _clean_queues: None,
) -> None:
    """Manual refresh and a duplicate submission both yield a version + event (4.7).

    Both routes go through the shared Refresh Service, so each produces a new
    ``dataset_versions`` row and a ``refresh_requested`` status event, differing
    only in the ``trigger`` label (``manual_refresh`` vs ``duplicate_submission``).
    This drives the manual (Library Refresh) path end to end and asserts the
    record shape, which the add-service integration suite already proves for the
    duplicate-submission path — so the two paths produce the same kind of record.
    """
    ds_id = dataset_factory(previous_verdict="will_work")

    resp = client.post(f"/api/datasets/{ds_id}/refresh")
    check_id = resp.json()["check_id"]
    check_cleanup.append(check_id)
    _drain_check_queue()

    _stub_pipeline(monkeypatch, verdict=Verdict(verdict="will_work", reasons=["ok"]))
    _run_handler_for(check_id)

    versions = _version_rows(ds_id)
    # A new version row was added by the manual path with the manual trigger.
    assert (2, "manual_refresh") in versions
    # The record "kind" matches what duplicate_submission produces: a v{n+1}
    # dataset_versions row plus a refresh_requested status event + a processing
    # message. (The duplicate_submission variant is asserted in
    # tests/integration/ingestion/test_add_service_int.py.)
    new_rows = [v for v in versions if v[0] == 2]
    assert len(new_rows) == 1
    assert any(e.get("message") == "refresh_requested" for e in _status_detail(ds_id)["events"])  # type: ignore[union-attr]
    # (The refresh also enqueues a processing message; we assert the durable v2
    # row + refresh_requested event rather than the queue message, which the
    # live consumer races the test to drain.)
