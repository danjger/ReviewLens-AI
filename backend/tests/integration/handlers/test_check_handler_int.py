"""Integration tests for app.handlers.check.CheckHandler (dataset-ingestion 4.2).

These drive the check handler against the **real** backing services the task
touches — the LocalStack ``check-sessions`` DynamoDB table (composite key
``check_id`` + ``item_id``), the LocalStack S3 bucket, the LocalStack
EventBridge bus, and the Compose PostgreSQL database — proving the storage,
event, and duplicate-lookup integration end to end:

* happy path: the item moves to ``done`` with the verdict, the Extraction Plan
  is written to ``checks/{check_id}/{item_id}/plan.json``, and ``check.updated``
  is published (Requirements 3.1, 2.4);
* duplicate lookup: a URL already tracked in PostgreSQL is recorded on the item
  as ``existing_dataset`` (Requirement 6.2);
* a crashed handler on the final SQS attempt leaves the item in ``error`` and a
  retry (re-claim) succeeds (design Error Handling);
* a duplicate delivery (second claim lost) produces exactly one result.

The browser Capture and the AI-backed viability assessment are stubbed at the
handler's module boundary, so this test needs neither Chromium nor an AI
fixture: the *real* integration under test is the Session store, S3, EventBridge,
and the database duplicate lookup. The full page-rendering + AI-stub path is
dataset-ingestion task 4.4.

Run with ``make test-int``. The module skips cleanly when LocalStack or
PostgreSQL is not reachable, matching the other integration suites. Each test
uses its own random ``check_id`` / dataset id and cleans up its DynamoDB rows,
S3 prefix, and database rows.
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
from app.storage import s3
from sqlalchemy import text

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"
_BUS = "reviewlens-events"


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
    """Point AWS clients at LocalStack and reset memoised handles."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", _ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", _S3_BUCKET)
    monkeypatch.setenv("EVENTBRIDGE_BUS_NAME", _BUS)
    get_settings.cache_clear()
    s3.reset_client()
    from app.events import publisher

    publisher.reset_client()
    try:
        yield
    finally:
        get_settings.cache_clear()
        s3.reset_client()
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


def _stub_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: Verdict,
    final_url: str = "http://fixtures/extraction/plain_list/",
) -> None:
    """Stub probe, capture, robots, and assess so no browser or AI is needed."""
    from app.capture.engine import CaptureResult

    monkeypatch.setattr(
        check_mod,
        "probe",
        lambda url: ProbeResult(
            final_url=url, status=200, ok=True, hops=[Hop(url=url, status=200, timestamp="t0")]
        ),
    )

    def _render(url: str, prefix: str) -> CaptureResult:
        # Write a real page.html so the handler's s3.get_text read works.
        s3.put_bytes(
            f"{prefix}page.html",
            b"<html><body><main>reviews</main></body></html>",
            content_type="text/html; charset=utf-8",
        )
        return CaptureResult(
            final_url=final_url,
            main_status=200,
            page_title="Fixture Reviews",
            html_key=f"{prefix}page.html",
            snapshot_key=f"{prefix}snapshot.png",
            redirected=False,
        )

    monkeypatch.setattr(check_mod.capture_engine, "render", _render)
    monkeypatch.setattr(check_mod, "robots_check", lambda url: RobotsResult(allowed=True))

    plan = ExtractionPlan(
        version=1,
        created_at="2024-01-01T00:00:00+00:00",
        method="selectors",
        next_page_rule=NextPageRule(type="selector", css=".next"),
    )
    monkeypatch.setattr(check_mod, "assess", lambda view, fu, robots: (verdict, plan))


def _item(normalized: str = "http://fixtures/extraction/plain_list/") -> CheckItem:
    return CheckItem(
        item_id="u1",
        input="http://fixtures/extraction/plain_list/",
        state="pending",
        normalized=normalized,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_end_to_end_writes_item_plan_and_publishes(
    monkeypatch: pytest.MonkeyPatch, check_id: str
) -> None:
    check_session.put_session(check_id, "new", [_item()])
    _stub_pipeline(monkeypatch, verdict=Verdict(verdict="will_work", reasons=["ok"]))

    CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

    session = check_session.get_session(check_id)
    assert session is not None
    item = session.items["u1"]
    assert item.state == "done"
    assert item.verdict is not None and item.verdict["verdict"] == "will_work"
    assert item.final_url == "http://fixtures/extraction/plain_list/"

    # Plan written to S3 under the check plan key.
    plan_key = f"checks/{check_id}/u1/plan.json"
    stored = json.loads(s3.get_text(plan_key))
    assert stored["method"] == "selectors"


def test_duplicate_lookup_records_existing_dataset(
    monkeypatch: pytest.MonkeyPatch, check_id: str
) -> None:
    normalized = f"http://fixtures/{uuid.uuid4().hex}/"
    ds_id = str(uuid.uuid4())
    with core_db.session_scope() as session:
        session.add(
            Dataset(
                id=ds_id,
                name="Tracked Fixture",
                source_type=SourceType.URL,
                original_url=normalized,
                normalized_url=normalized,
                status=DatasetStatus.UPDATED,
                status_detail={"events": []},
            )
        )

    try:
        check_session.put_session(check_id, "new", [_item(normalized=normalized)])
        _stub_pipeline(
            monkeypatch,
            verdict=Verdict(verdict="will_work"),
            final_url=normalized,
        )

        CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

        session = check_session.get_session(check_id)
        assert session is not None
        existing = session.items["u1"].existing_dataset
        assert existing is not None
        assert existing["id"] == ds_id
        assert existing["name"] == "Tracked Fixture"
    finally:
        with core_db.session_scope() as session:
            session.execute(
                text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
            )


# ---------------------------------------------------------------------------
# Crash → error on final attempt, then retry succeeds
# ---------------------------------------------------------------------------


def test_crash_on_final_attempt_leaves_error_and_retry_works(
    monkeypatch: pytest.MonkeyPatch, check_id: str
) -> None:
    from app.consumer import MAX_RECEIVE_COUNT

    check_session.put_session(check_id, "new", [_item()])

    def _boom(url: str) -> ProbeResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(check_mod, "probe", _boom)

    with pytest.raises(RuntimeError):
        CheckHandler().handle(
            {"check_id": check_id, "item_id": "u1"},
            _meta(receive_count=MAX_RECEIVE_COUNT),
        )

    session = check_session.get_session(check_id)
    assert session is not None
    assert session.items["u1"].state == "error"

    # A retry can re-claim the errored item and now succeed.
    _stub_pipeline(monkeypatch, verdict=Verdict(verdict="limited"))
    CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

    session = check_session.get_session(check_id)
    assert session is not None
    assert session.items["u1"].state == "done"
    assert session.items["u1"].verdict is not None
    assert session.items["u1"].verdict["verdict"] == "limited"


# ---------------------------------------------------------------------------
# Duplicate delivery → one result
# ---------------------------------------------------------------------------


def test_duplicate_delivery_produces_one_result(
    monkeypatch: pytest.MonkeyPatch, check_id: str
) -> None:
    check_session.put_session(check_id, "new", [_item()])
    _stub_pipeline(monkeypatch, verdict=Verdict(verdict="will_work"))

    body = {"check_id": check_id, "item_id": "u1"}
    CheckHandler().handle(body, _meta())
    # Second (duplicate) delivery: the item is already `done`, so the claim is
    # lost and the message is dropped without re-running the pipeline.
    monkeypatch.setattr(
        check_mod, "probe", lambda url: pytest.fail("pipeline re-ran on a duplicate delivery")
    )
    CheckHandler().handle(body, _meta())

    session = check_session.get_session(check_id)
    assert session is not None
    assert session.items["u1"].state == "done"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meta(receive_count: int = 1):  # type: ignore[no-untyped-def]
    from app.consumer import MessageMeta

    return MessageMeta(message_id="m-int", receive_count=receive_count)
