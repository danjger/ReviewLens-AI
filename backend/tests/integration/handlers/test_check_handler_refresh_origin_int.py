"""Refresh-origin check-handler end-to-end integration tests (dataset-ingestion 6.3).

Task 6.2's unit tests (``tests/unit/handlers/test_check_handler.py``) proved the
refresh-origin *decisions* (Requirement 6.9) with the Refresh Service and
``log_event`` faked at the module boundary. This suite closes the loop the
design's Testing Strategy calls for — "Refresh-origin checks: ``will_work`` ->
refresh starts with no Add call; ``wont_work`` -> no version row, failed-refresh
event" — by driving the **whole** refresh-origin path against the real backing
services:

* the LocalStack ``check-sessions`` DynamoDB table (an ``origin="refresh"``
  one-item session carrying ``refresh_dataset_id``);
* the LocalStack S3 bucket (the fresh plan the handler stores, then the capture
  the Refresh Service copies into ``raw/v{n}``);
* the Compose PostgreSQL database (the dataset, its ``data_version`` /
  ``status`` / ``status_detail``, and the ``dataset_versions`` rows);
* the LocalStack SQS FIFO ``processing-queue.fifo``.

Only the heavy *inputs* — probe, browser capture, robots, and the AI-backed
viability assessment — are stubbed at the handler's module boundary (exactly as
``test_check_handler_int.py`` does for the ``new`` path), so this needs neither
Chromium nor an AI fixture. The real integration under test is the
handler -> Refresh Service -> database/S3/SQS wiring and the no-version
``wont_work`` branch.

What it proves (Requirement 6.9):

1. **``will_work`` auto-refreshes**: without any Add call, the dataset gets a
   new ``data_version`` (v+1), a ``dataset_versions`` row, a
   ``refresh_requested`` transition, the capture copied into ``raw/v{n}``, a
   processing message enqueued, and the item ends ``applied``.
2. **``wont_work`` creates no version**: no new ``dataset_versions`` row, the
   ``data_version`` and ``status`` are unchanged, a failed-refresh event is
   appended to ``status_detail`` (no transition), and the item ends ``done``.

Running
-------
Run with ``make test-int`` under ``make up`` (LocalStack **and** PostgreSQL).
The module and each test skip cleanly when either is unavailable, so the suite
still collects without the stack. Each test uses its own random ids and cleans
up its DynamoDB rows, S3 prefix, and database rows.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import boto3
import httpx
import pytest
from app.core import db as core_db
from app.core.config import get_settings
from app.db.models import Base, Dataset, DatasetStatus, SourceType
from app.extraction.models import ExtractionPlan, NextPageRule
from app.handlers import check as check_mod
from app.handlers.check import CheckHandler
from app.ingestion import check_session
from app.ingestion.check_session import CheckItem
from app.ingestion.robots import RobotsResult
from app.ingestion.url_validator import Hop, ProbeResult
from app.ingestion.verdict import Verdict
from app.storage import keys, s3
from sqlalchemy import text

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"
_PROCESSING_QUEUE_URL = (
    "http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/processing-queue.fifo"
)


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
def check_id() -> Iterator[str]:
    """A fresh check_id; delete its DynamoDB rows and S3 prefix afterwards."""
    cid = f"it-{uuid.uuid4().hex}"
    try:
        yield cid
    finally:
        ddb = boto3.client("dynamodb", region_name=_REGION, endpoint_url=_ENDPOINT)
        resp = ddb.query(
            TableName="check-sessions",
            KeyConditionExpression="check_id = :c",
            ExpressionAttributeValues={":c": {"S": cid}},
        )
        for row in resp.get("Items", []):
            ddb.delete_item(
                TableName="check-sessions",
                Key={"check_id": row["check_id"], "item_id": row["item_id"]},
            )
        client = s3._get_s3_client()
        listed = client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=f"checks/{cid}/")
        for obj in listed.get("Contents", []):
            key = obj.get("Key")
            if key:
                client.delete_object(Bucket=_S3_BUCKET, Key=key)


@pytest.fixture()
def dataset_id() -> Iterator[str]:
    """Insert a tracked `updated` URL dataset with a prior verdict; clean up after."""
    ds_id = str(uuid.uuid4())

    def _make(previous_verdict: str = "will_work") -> str:
        with core_db.session_scope() as session:
            session.add(
                Dataset(
                    id=ds_id,
                    name="Tracked Fixture",
                    source_type=SourceType.URL,
                    original_url=f"https://example.com/{ds_id}",
                    normalized_url=f"https://example.com/{ds_id}",
                    status=DatasetStatus.UPDATED,
                    status_detail={"events": [], "viability": {"verdict": previous_verdict}},
                    data_version=1,
                )
            )
            session.execute(
                text(
                    "INSERT INTO dataset_versions (dataset_id, version, trigger, requested_at) "
                    "VALUES (CAST(:id AS uuid), 1, 'initial', now())"
                ),
                {"id": ds_id},
            )
        return ds_id

    try:
        yield _make  # type: ignore[misc]
    finally:
        client = s3._get_s3_client()
        with core_db.session_scope() as session:
            session.execute(
                text("DELETE FROM dataset_versions WHERE dataset_id = CAST(:id AS uuid)"),
                {"id": ds_id},
            )
            session.execute(
                text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
            )
        listed = client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=keys.dataset_prefix(ds_id))
        for obj in listed.get("Contents", []):
            key = obj.get("Key")
            if key:
                client.delete_object(Bucket=_S3_BUCKET, Key=key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meta(receive_count: int = 1):  # type: ignore[no-untyped-def]
    from app.consumer import MessageMeta

    return MessageMeta(message_id="m-refresh-int", receive_count=receive_count)


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch, *, verdict: Verdict) -> None:
    """Stub probe/capture/robots/assess so the pipeline needs no browser or AI.

    The stubbed capture writes a real ``page.html`` under the item's prefix so
    the handler's ``s3.get_text`` read and the Refresh Service's later copy of
    the page both work against real S3.
    """
    from app.capture.engine import CaptureResult

    url = "https://example.com/original"
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
            final_url=url,
            main_status=200,
            page_title="Tracked Fixture",
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


def _refresh_item(ds_id: str) -> CheckItem:
    return CheckItem(
        item_id="u1",
        input=f"https://example.com/{ds_id}",
        state="pending",
        normalized=f"https://example.com/{ds_id}",
    )


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


def _status_events(ds_id: str) -> list[dict[str, object]]:
    with core_db.session_scope() as session:
        detail = session.execute(
            text("SELECT status_detail FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
        ).scalar_one()
    assert isinstance(detail, dict)
    events = detail.get("events", [])
    assert isinstance(events, list)
    return events


def _drain_processing_queue() -> list[dict[str, object]]:
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
    _drain_processing_queue()
    yield
    _drain_processing_queue()


# ---------------------------------------------------------------------------
# will_work -> auto-refresh, new version, item applied (Requirement 6.9)
# ---------------------------------------------------------------------------


def test_refresh_origin_will_work_creates_v2_and_marks_applied(
    monkeypatch: pytest.MonkeyPatch,
    check_id: str,
    dataset_id: object,
    _clean_queue: None,
) -> None:
    ds_id = dataset_id("will_work")  # type: ignore[operator]
    check_session.put_session(check_id, "refresh", [_refresh_item(ds_id)], refresh_dataset_id=ds_id)
    _stub_pipeline(monkeypatch, verdict=Verdict(verdict="will_work", reasons=["24 verified"]))

    CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

    # A new data version was created automatically — no Add call needed.
    db_status, version = _dataset_row(ds_id)
    assert version == 2
    assert db_status == DatasetStatus.REQUESTED.value
    assert (2, "manual_refresh") in _version_rows(ds_id)

    # The capture was copied into raw/v2 by the Refresh Service.
    assert s3.object_exists(keys.dataset_raw_page(ds_id, 2, 1))
    assert s3.object_exists(keys.dataset_raw_plan(ds_id, 2))

    # refresh_requested event appended and a processing message enqueued.
    assert any(e.get("message") == "refresh_requested" for e in _status_events(ds_id))
    assert {"dataset_id": ds_id, "data_version": 2} in _drain_processing_queue()

    # The check item finished `applied`.
    session = check_session.get_session(check_id)
    assert session is not None
    assert session.items["u1"].state == "applied"


# ---------------------------------------------------------------------------
# wont_work -> no new version, failed-refresh event, item done (Requirement 6.9)
# ---------------------------------------------------------------------------


def test_refresh_origin_wont_work_creates_no_version_and_logs_failure(
    monkeypatch: pytest.MonkeyPatch,
    check_id: str,
    dataset_id: object,
    _clean_queue: None,
) -> None:
    ds_id = dataset_id("will_work")  # type: ignore[operator]
    check_session.put_session(check_id, "refresh", [_refresh_item(ds_id)], refresh_dataset_id=ds_id)
    _stub_pipeline(
        monkeypatch,
        verdict=Verdict(verdict="wont_work", reasons=["The page is a bot or CAPTCHA challenge"]),
    )

    CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

    # No new data version: data_version and status are unchanged.
    db_status, version = _dataset_row(ds_id)
    assert version == 1
    assert db_status == DatasetStatus.UPDATED.value
    # Only the seeded v1 row exists — no v2 was inserted.
    assert _version_rows(ds_id) == [(1, "initial")]

    # A failed-refresh event was appended (no status transition, so no
    # `refresh_requested` and no `requested` status event).
    events = _status_events(ds_id)
    assert any("Refresh failed" in str(e.get("message", "")) for e in events)
    assert not any(e.get("message") == "refresh_requested" for e in events)

    # Nothing enqueued for processing.
    assert _drain_processing_queue() == []

    # The check item finished `done` with the wont_work verdict.
    session = check_session.get_session(check_id)
    assert session is not None
    item = session.items["u1"]
    assert item.state == "done"
    assert item.verdict is not None
    assert item.verdict["verdict"] == "wont_work"
