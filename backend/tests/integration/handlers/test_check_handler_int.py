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
# Upload-item branch (dataset-ingestion task 18 / Requirements 8.3, 8.4, 8.13)
# ---------------------------------------------------------------------------


_UPLOAD_HTML = (
    b"<html><head><title>Saved Reviews</title></head>"
    b"<body><main><div class='review'><p>A genuinely great product.</p></div></main></body></html>"
)


def _stub_assess_only(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: Verdict,
) -> tuple[list[str], list[str], list]:
    """Stub ``assess`` and spy on ``probe``/``robots_check`` for the upload path.

    The upload branch must *never* probe, follow redirects, or consult robots.txt
    (nothing is fetched — Requirement 8.5), so ``probe`` and ``robots_check`` are
    replaced with spies that record (and fail) if called. ``assess`` is stubbed
    so no AI/extraction runs; the real :func:`app.capture.from_upload` still runs
    against a staged S3 object. Returns ``(probe_calls, robots_calls, assess_calls)``.
    """
    probe_calls: list[str] = []
    robots_calls: list[str] = []
    assess_calls: list = []

    def _spy_probe(url: str):  # type: ignore[no-untyped-def]
        probe_calls.append(url)
        pytest.fail("probe must not be called on the upload path")

    def _spy_robots(url: str):  # type: ignore[no-untyped-def]
        robots_calls.append(url)
        pytest.fail("robots_check must not be called on the upload path")

    monkeypatch.setattr(check_mod, "probe", _spy_probe)
    monkeypatch.setattr(check_mod, "robots_check", _spy_robots)

    plan = ExtractionPlan(
        version=1,
        created_at="2024-01-01T00:00:00+00:00",
        method="selectors",
        next_page_rule=NextPageRule(type="none"),
    )

    def _assess(view, final_url, robots):  # type: ignore[no-untyped-def]
        assess_calls.append((view, final_url, robots))
        return verdict, plan

    monkeypatch.setattr(check_mod, "assess", _assess)
    return probe_calls, robots_calls, assess_calls


def _spy_check_updated(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture ``check.updated`` event details the handler publishes."""
    captured: list[dict] = []

    def _fake_publish(*, detail_type: str, detail: dict) -> None:  # type: ignore[no-untyped-def]
        if detail_type == check_mod.CHECK_UPDATED_DETAIL_TYPE:
            captured.append(detail)

    monkeypatch.setattr(check_mod, "publish_event", _fake_publish)
    return captured


def _put_upload_file(upload_id: str, body: bytes = _UPLOAD_HTML) -> None:
    """Stage a saved-page HTML file at ``uploads/{upload_id}/file`` in S3."""
    from app.storage import keys

    s3.put_bytes(keys.upload_file(upload_id), body, content_type="text/html")


def _upload_item(upload_id: str, *, source_url: str | None = None) -> CheckItem:
    """A one-item upload CheckItem carrying ``upload_id`` (and optional source)."""
    normalized = None
    if source_url is not None:
        from app.ingestion.url_normalizer import normalize

        normalized = normalize(source_url)
    return CheckItem(
        item_id="u1",
        input="saved-page.html",
        state="pending",
        upload_id=upload_id,
        source_url=source_url,
        normalized=normalized,
    )


def test_upload_item_reaches_verdict_plan_event_no_probe_or_robots(
    monkeypatch: pytest.MonkeyPatch, check_id: str
) -> None:
    """An upload item is assessed with no probe/robots fetch; verdict, plan, event (8.3)."""
    upload_id = f"up-{uuid.uuid4().hex}"
    _put_upload_file(upload_id)
    check_session.put_session(check_id, "new", [_upload_item(upload_id)])

    probe_calls, robots_calls, assess_calls = _stub_assess_only(
        monkeypatch, verdict=Verdict(verdict="will_work", reasons=["ok"])
    )
    events = _spy_check_updated(monkeypatch)

    CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

    # Nothing was fetched: probe and robots were never called (Requirement 8.5).
    assert probe_calls == []
    assert robots_calls == []

    # assess() ran with a "no restriction" robots result and the synthetic
    # final_url placeholder (no source URL was supplied) — Requirement 8.13.
    assert len(assess_calls) == 1
    _view, final_url, robots = assess_calls[0]
    assert robots.allowed is True
    assert final_url == f"upload://{upload_id}"

    session = check_session.get_session(check_id)
    assert session is not None
    item = session.items["u1"]
    assert item.state == "done"
    assert item.verdict is not None and item.verdict["verdict"] == "will_work"
    assert item.final_url == f"upload://{upload_id}"

    # The uploaded HTML was copied under the check page key, and the plan written.
    page = s3.get_text(f"checks/{check_id}/u1/page.html")
    assert "A genuinely great product." in page
    plan = json.loads(s3.get_text(f"checks/{check_id}/u1/plan.json"))
    assert plan["method"] == "selectors"

    # check.updated was published with the done state.
    assert any(e.get("item_id") == "u1" and e.get("state") == "done" for e in events)


def test_upload_item_without_source_url_skips_duplicate_lookup(
    monkeypatch: pytest.MonkeyPatch, check_id: str
) -> None:
    """With no source URL the duplicate lookup never runs (Requirement 8.11)."""
    import app.ingestion.duplicates as duplicates_mod

    upload_id = f"up-{uuid.uuid4().hex}"
    _put_upload_file(upload_id)
    check_session.put_session(check_id, "new", [_upload_item(upload_id)])

    _stub_assess_only(monkeypatch, verdict=Verdict(verdict="will_work"))

    find_calls: list = []
    real_find = duplicates_mod.find_existing

    def _spy_find(*args, **kwargs):  # type: ignore[no-untyped-def]
        find_calls.append((args, kwargs))
        return real_find(*args, **kwargs)

    monkeypatch.setattr(check_mod.duplicates, "find_existing", _spy_find)

    CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

    assert find_calls == [], "duplicate lookup must be skipped when no source_url is present"

    session = check_session.get_session(check_id)
    assert session is not None
    assert session.items["u1"].existing_dataset is None


def test_upload_item_with_tracked_source_url_records_existing_dataset(
    monkeypatch: pytest.MonkeyPatch, check_id: str
) -> None:
    """A supplied source URL that is tracked is recorded via the duplicate lookup (8.12)."""
    from app.ingestion.url_normalizer import normalize

    source_url = f"http://fixtures/{uuid.uuid4().hex}/"
    normalized = normalize(source_url)
    ds_id = str(uuid.uuid4())
    with core_db.session_scope() as session:
        session.add(
            Dataset(
                id=ds_id,
                name="Tracked Page",
                source_type=SourceType.URL,
                original_url=normalized,
                normalized_url=normalized,
                status=DatasetStatus.UPDATED,
                status_detail={"events": []},
            )
        )

    try:
        upload_id = f"up-{uuid.uuid4().hex}"
        _put_upload_file(upload_id)
        check_session.put_session(check_id, "new", [_upload_item(upload_id, source_url=source_url)])

        probe_calls, robots_calls, assess_calls = _stub_assess_only(
            monkeypatch, verdict=Verdict(verdict="will_work")
        )

        CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

        # Still no fetch on the upload path.
        assert probe_calls == []
        assert robots_calls == []
        # assess received the supplied source URL as the (never-fetched) final_url.
        _view, final_url, _robots = assess_calls[0]
        assert final_url == source_url

        session = check_session.get_session(check_id)
        assert session is not None
        existing = session.items["u1"].existing_dataset
        assert existing is not None
        assert existing["id"] == ds_id
        assert existing["name"] == "Tracked Page"
    finally:
        with core_db.session_scope() as session:
            session.execute(
                text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meta(receive_count: int = 1):  # type: ignore[no-untyped-def]
    from app.consumer import MessageMeta

    return MessageMeta(message_id="m-int", receive_count=receive_count)
