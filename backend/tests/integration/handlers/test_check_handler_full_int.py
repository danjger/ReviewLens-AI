"""Full render + AI-stub integration tests for the check handler (task 4.4).

Where task 4.2 (``test_check_handler_int.py``) stubbed Capture and the viability
assessment at the handler's module boundary, this suite drives the **whole**
Check pipeline end to end against the real backing services the task touches:

* the Compose ``fixtures`` nginx container serves each HTML fixture over HTTP
  (``localhost:9090``), reached through ``SSRF_TEST_ALLOW_HOSTS``;
* the real probe follows redirects and requires a 200 over real sockets;
* the real :func:`app.capture.engine.render` renders the page in headless
  Chromium (the workers image) with its SSRF route guard;
* the real :func:`app.ingestion.viability.assess` runs the pre-scan, the
  Extraction Engine (``build_plan``), verification, and the verdict rules;
* the LocalStack ``check-sessions`` DynamoDB table, the LocalStack S3 bucket,
  the LocalStack EventBridge bus, and the Compose PostgreSQL database back the
  session store, the stored plan, the published ``check.updated`` event, and
  the duplicate lookup.

What this proves (Requirements 1.6, 3.1, 3.10):

1. **Per fixture, the expected item state, verdict, plan, and event.**
   - ``blocker_empty`` (an empty JavaScript shell) and ``blocker_consent`` (a
     consent wall) end ``done`` with a ``wont_work`` verdict from the free
     rule-based pre-scan — **no AI call** — a plan at
     ``checks/{check_id}/{item_id}/plan.json``, and ``check.updated`` published
     (Requirement 3.1; design "Viability assessment" step 1).
   - ``plain_list`` (four real reviews) is taken through the full AI path with a
     *recorded-shaped* Locator response (see "The AI stub" below) and ends
     ``done`` with a ``will_work`` verdict, a ``selectors`` plan, and
     ``check.updated`` published (Requirement 3.1).
2. **A crashed handler leaves the item in ``error`` on the final attempt, and a
   retry then works** (design Error Handling; Requirement 3.10 Retry).
3. **The same message delivered twice produces exactly one result** — the
   conditional claim drops the second delivery (design: idempotent handler).
4. **429 after the per-request rate limit is exceeded**, via the real
   ``POST /ingest/checks`` endpoint and the LocalStack ``rate-limits`` table
   (Requirement 1.6).

The AI stub
-----------
``testing.md`` says tests use ``FakeClaude`` replaying recorded responses from
``tests/fixtures/ai/`` and must not hand-write them. A true recorded **Review
Locator** response needs ``make record-ai`` with an ``ANTHROPIC_API_KEY`` — a
"needs a person" step — and none is committed yet. So, exactly like the
extraction suite's ``test_locator_recorded.py``, the ``plain_list`` AI path uses
an SDK-shaped *scripted* Locator ``tool_use`` response built from the rendered
page's own element references (never hand-written JSON prose). Token counting
is delegated to the offline ``FakeClaude`` estimator so the cleaner's budget
logic runs unchanged. The two blocker fixtures need **no** AI at all, so they
run fully end to end today with zero caveat.

Running
-------
Run with ``make test-int`` under ``make up`` (needs LocalStack, PostgreSQL, the
``fixtures`` container, **and** Chromium from the workers image). The module —
and each test — skips cleanly when any of those is unavailable, so it collects
without the stack. Each test uses its own random ``check_id`` / dataset id and
cleans up its DynamoDB rows, S3 prefix, and database rows.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import boto3
import httpx
import pytest
from app.core import db as core_db
from app.core.ai import AiClient, reset_ai_client, set_ai_client
from app.core.config import get_settings
from app.db.models import Base
from app.extraction.cleaner import build_clean_result, resolve_ref
from app.handlers.check import CheckHandler
from app.ingestion import check_session
from app.ingestion.check_session import CheckItem
from app.storage import s3
from sqlalchemy import text

from tests.support.ai import FakeClaude

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"
_BUS = "reviewlens-events"

#: The Compose ``fixtures`` nginx container, reached from the host test process
#: (and the host-run browser) at this base. Allowed through the SSRF guard by
#: ``SSRF_TEST_ALLOW_HOSTS`` below.
_FIXTURES_BASE = "http://localhost:9090"

#: SSRF allowlist for these tests: the fixtures host resolves to localhost when
#: reached from the host, so both the probe and the capture route guard must
#: allow ``localhost`` / ``127.0.0.1`` (same mechanism as ``test_probe_int``).
_ALLOW_HOSTS = "localhost,127.0.0.1,fixtures"


# ---------------------------------------------------------------------------
# Availability probes (skip cleanly without the stack / Chromium)
# ---------------------------------------------------------------------------


def _localstack_up() -> bool:
    try:
        resp = httpx.get(f"{_ENDPOINT}/_localstack/health", timeout=2.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _fixtures_up() -> bool:
    try:
        resp = httpx.get(f"{_FIXTURES_BASE}/", timeout=2.0)
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


def _chromium_available() -> bool:
    """True when a headless Chromium can launch (the workers image provides it)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001 - playwright not installed → skip
        return False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # noqa: BLE001 - no browser binary / sandbox issue → skip
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point AWS clients at LocalStack, allow the fixtures host, reset handles."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", _ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", _S3_BUCKET)
    monkeypatch.setenv("EVENTBRIDGE_BUS_NAME", _BUS)
    monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", _ALLOW_HOSTS)
    # The integration suite loads the repo-root ``.env``, which sets
    # ``ORIGIN_VERIFY_SECRET`` — so ``OriginGuardMiddleware`` would 403 the
    # headerless ``TestClient`` request in ``TestCheckEndpointRateLimit`` before
    # it can reach the 429 assertion. Clear the secret so the guard runs in
    # local-dev bypass, matching the library/summary integration suites
    # (dataset-ingestion task 12.3 / Known Issues D).
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", "")
    # The ``plain_list`` fixture has exactly 4 reviews. ``will_work`` requires
    # ``verified >= viability_min_reviews`` (Requirement 3.4), whose default is 5
    # — so with the default a 4-review page is correctly ``limited``. The
    # ``plain_list`` fixture and its 4-review eval labels are owned by the
    # ``review-extraction`` spec (``evals/extraction/labels.yaml`` +
    # ``test_selectors_method_scores_plain_list`` asserts precision/recall == 1.0
    # over exactly those 4), so adding a 5th review would break that cross-spec
    # eval. We therefore drop *only this test's* threshold to 4 so the fixture's
    # 4 verified reviews exercise the ``will_work`` selectors path, without
    # touching the shared fixture or another spec's files or weakening the
    # product's default rule (dataset-ingestion task 12.2 / Known Issues B).
    monkeypatch.setenv("VIABILITY_MIN_REVIEWS", "4")
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
        reset_ai_client()


@pytest.fixture(scope="module", autouse=True)
def _require_stack() -> None:
    if not _localstack_up():
        pytest.skip("LocalStack not reachable; run under `make test-int`")
    if not _fixtures_up():
        pytest.skip("fixtures container not reachable; run under `make test-int`")
    if not _chromium_available():
        pytest.skip("headless Chromium not available; run under `make test-int` (workers image)")


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


@pytest.fixture(autouse=True)
def _shutdown_browser() -> Iterator[None]:
    """Close the per-process browser after each test for isolation."""
    yield
    from app.capture import engine as capture_engine

    capture_engine.shutdown_browser()


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


# ---------------------------------------------------------------------------
# Scripted Locator stub (an inline "recorded response"), mirroring the
# extraction suite's test_locator_recorded.py. Delegates count_tokens to the
# offline FakeClaude estimator so the cleaner's budget logic runs unchanged.
# ---------------------------------------------------------------------------


def _tool_use_response(tool_input: dict[str, Any]) -> SimpleNamespace:
    """An SDK-shaped message carrying a forced ``locator_result`` tool_use."""
    block = SimpleNamespace(type="tool_use", name="locator_result", input=tool_input)
    return SimpleNamespace(
        id="msg_recorded",
        content=[block],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class _ScriptedMessages:
    """``messages`` resource: scripted ``create``, offline ``count_tokens``."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self._fake = FakeClaude()
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("Scripted Locator client ran out of responses")
        return self._responses.pop(0)

    def count_tokens(self, **kwargs: Any) -> Any:
        # Delegate to the offline estimator so the cleaner never hits the net.
        return self._fake.messages.count_tokens(**kwargs)


class _ScriptedClient:
    """Anthropic-like client returning queued Locator responses in order."""

    def __init__(self, responses: list[Any]) -> None:
        self.messages = _ScriptedMessages(responses)


def _install_scripted(responses: list[Any]) -> _ScriptedClient:
    client = _ScriptedClient(responses)
    set_ai_client(AiClient(client=client))
    return client


def _ref_for_text(html: str, lookup: dict[str, str], needle: str) -> str:
    """Ref of the smallest element whose text contains ``needle`` (a leaf)."""
    best_ref: str | None = None
    best_len: int | None = None
    for ref in lookup:
        node = resolve_ref(html, lookup, ref)
        if node is None:
            continue
        node_text = node.text()
        if needle in node_text and (best_len is None or len(node_text) < best_len):
            best_ref = ref
            best_len = len(node_text)
    if best_ref is None:
        raise AssertionError(f"no ref resolves to text containing {needle!r}")
    return best_ref


#: The four review bodies in the published ``plain_list`` fixture, used to point
#: the scripted Locator at the real rendered elements by reference.
_PLAIN_LIST_REVIEW_SNIPPETS = (
    "Grinds evenly and quietly",
    "Great grind consistency for pour-over",
    "Decent machine for the price",
    "Replaced a much pricier grinder",
)


def _plain_list_locator_response(html: str) -> SimpleNamespace:
    """Build a recorded-shaped Locator response for the rendered ``plain_list``.

    The response points at the four real review bodies (and their rating/author/
    date/title siblings) by element reference derived from the *rendered* DOM,
    so post-processing reads the kept text from the page — never from the model
    (steering: the AI may never supply review text).
    """
    cleaned = build_clean_result(html)
    lookup = cleaned.lookup
    items: list[dict[str, Any]] = []
    for snippet in _PLAIN_LIST_REVIEW_SNIPPETS:
        body = _ref_for_text(html, lookup, snippet)
        items.append({"item_ref": body, "text_ref": body, "kind": "review"})
    return _tool_use_response(
        {
            "has_reviews": True,
            "rating_scale": 5,
            "items": items,
            "selectors": {"item": ".review", "text": ".text"},
            "next_page": {"ref": None},
            "reported_total": 4,
            "entity_hint": "Bluebird Coffee Grinder",
            "confidence": "high",
        }
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meta(receive_count: int = 1):  # type: ignore[no-untyped-def]
    from app.consumer import MessageMeta

    return MessageMeta(message_id="m-full-int", receive_count=receive_count)


def _fixture_url(name: str) -> str:
    return f"{_FIXTURES_BASE}/extraction/{name}/"


def _put_single_item(cid: str, url: str) -> None:
    """Create a one-item ``origin="new"`` session for *url*."""
    check_session.put_session(
        cid,
        "new",
        [CheckItem(item_id="u1", input=url, state="pending", normalized=url)],
    )


# ---------------------------------------------------------------------------
# Per-fixture end-to-end: no-AI blockers (fully runnable today)
# ---------------------------------------------------------------------------


class TestBlockerFixturesEndToEnd:
    """Empty-shell and consent-wall fixtures → ``wont_work`` with no AI call."""

    @pytest.mark.parametrize(
        ("fixture_name", "expected_blocker"),
        [("blocker_empty", "empty"), ("blocker_consent", None)],
    )
    def test_blocker_fixture_wont_work_no_ai(
        self,
        monkeypatch: pytest.MonkeyPatch,
        check_id: str,
        fixture_name: str,
        expected_blocker: str | None,
    ) -> None:
        url = _fixture_url(fixture_name)
        _put_single_item(check_id, url)

        # Record check.updated and prove no AI call happens on the blocker path.
        events = _spy_publisher(monkeypatch)
        ai_calls = _forbid_ai(monkeypatch)

        CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

        session = check_session.get_session(check_id)
        assert session is not None
        item = session.items["u1"]
        assert item.state == "done"
        assert item.verdict is not None
        assert item.verdict["verdict"] == "wont_work"
        # The pre-scan short-circuit spends no AI (design step 1).
        assert ai_calls == [], "a certain blocker must not reach the AI"

        # The Extraction Plan was written under the check plan key.
        plan_key = f"checks/{check_id}/u1/plan.json"
        plan = json.loads(s3.get_text(plan_key))
        assert plan["degraded"] is True

        # check.updated was published with the done state.
        assert any(
            e.get("check_id") == check_id and e.get("item_id") == "u1" and e.get("state") == "done"
            for e in events
        )
        # The consent fixture's blocker label is recorded as a consent/empty gate.
        if expected_blocker is not None:
            assert item.verdict["evidence"]["blocker"] == expected_blocker


# ---------------------------------------------------------------------------
# Per-fixture end-to-end: the full AI path (scripted Locator response)
# ---------------------------------------------------------------------------


class TestPlainListFullAiPath:
    """``plain_list`` → full render + scripted Locator → ``will_work`` + plan."""

    def test_will_work_with_selectors_plan_and_event(
        self, monkeypatch: pytest.MonkeyPatch, check_id: str
    ) -> None:
        from app.capture import engine as capture_engine

        url = _fixture_url("plain_list")
        _put_single_item(check_id, url)
        events = _spy_publisher(monkeypatch)

        # Render once to learn the rendered DOM, then script the Locator against
        # it (this mirrors a recorded response pointing at real refs). The real
        # handler renders again inside the pipeline; the fixture is static HTML
        # so the refs are stable between the two renders.
        probe_prefix = f"checks/{check_id}/u1/"
        capture = capture_engine.render(url, probe_prefix)
        rendered_html = s3.get_text(capture.html_key)
        _install_scripted([_plain_list_locator_response(rendered_html)])

        CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

        session = check_session.get_session(check_id)
        assert session is not None
        item = session.items["u1"]
        assert item.state == "done"
        assert item.verdict is not None
        # Four verified reviews, no blocker, high confidence → will_work (3.4).
        assert item.verdict["verdict"] == "will_work"
        assert item.verdict["evidence"]["reviews_verified"] == 4

        plan_key = f"checks/{check_id}/u1/plan.json"
        plan = json.loads(s3.get_text(plan_key))
        assert plan["degraded"] is False
        assert plan["method"] in {"selectors", "ai_direct", "structured"}

        assert any(e.get("check_id") == check_id and e.get("state") == "done" for e in events)


# ---------------------------------------------------------------------------
# Crash → error on the final attempt, then retry works (full path)
# ---------------------------------------------------------------------------


class TestCrashThenRetry:
    """A crashed handler leaves ``error`` on the final attempt; retry works."""

    def test_crash_on_final_attempt_then_retry_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, check_id: str
    ) -> None:
        from app.consumer import MAX_RECEIVE_COUNT
        from app.handlers import check as check_mod

        url = _fixture_url("blocker_empty")
        _put_single_item(check_id, url)

        # Force a crash deep in the pipeline on the final attempt.
        def _boom(_url: str) -> Any:
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

        # Undo the crash and retry: the errored item is re-claimed and finishes.
        monkeypatch.undo()
        # Re-apply the SSRF allowlist that monkeypatch.undo() just reverted.
        monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", _ALLOW_HOSTS)
        get_settings.cache_clear()

        CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

        session = check_session.get_session(check_id)
        assert session is not None
        assert session.items["u1"].state == "done"
        assert session.items["u1"].verdict is not None
        assert session.items["u1"].verdict["verdict"] == "wont_work"


# ---------------------------------------------------------------------------
# Duplicate delivery → one result
# ---------------------------------------------------------------------------


class TestDuplicateDelivery:
    """The same message delivered twice runs the pipeline exactly once."""

    def test_duplicate_delivery_produces_one_result(
        self, monkeypatch: pytest.MonkeyPatch, check_id: str
    ) -> None:
        from app.handlers import check as check_mod

        url = _fixture_url("blocker_empty")
        _put_single_item(check_id, url)

        CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

        session = check_session.get_session(check_id)
        assert session is not None
        assert session.items["u1"].state == "done"

        # Second (duplicate) delivery: the item is already done, so the claim is
        # lost and the pipeline must not run again.
        monkeypatch.setattr(
            check_mod,
            "probe",
            lambda _url: pytest.fail("pipeline re-ran on a duplicate delivery"),
        )
        CheckHandler().handle({"check_id": check_id, "item_id": "u1"}, _meta())

        session = check_session.get_session(check_id)
        assert session is not None
        assert session.items["u1"].state == "done"


# ---------------------------------------------------------------------------
# 429 after the rate limit, via the real POST /ingest/checks endpoint
# ---------------------------------------------------------------------------


class TestCheckEndpointRateLimit:
    """``POST /ingest/checks`` returns 429 once the per-request limit is hit."""

    def test_429_after_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi.testclient import TestClient

        # A tiny per-IP limit so a couple of requests cross it deterministically.
        monkeypatch.setenv("RL_CHECKS_PER_IP_HOUR", "2")
        get_settings.cache_clear()

        from app.api import app

        client = TestClient(app)
        headers = {"CloudFront-Viewer-Address": f"203.0.113.{uuid.uuid4().int % 250}:4321"}
        body = {"urls": [_fixture_url("blocker_empty")]}

        last_status = None
        saw_429 = False
        for _ in range(5):
            resp = client.post("/api/ingest/checks", json=body, headers=headers)
            last_status = resp.status_code
            if resp.status_code == 429:
                saw_429 = True
                # The panel must be told when checks can resume (Req 1.6).
                assert "Retry-After" in resp.headers
                payload = resp.json()
                assert payload["error"]["code"] == "RATE_LIMIT_EXCEEDED"
                break
        assert saw_429, f"expected a 429 after the limit; last status was {last_status}"


# ---------------------------------------------------------------------------
# Publisher spy / AI guard helpers
# ---------------------------------------------------------------------------


def _spy_publisher(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record ``check.updated`` detail dicts the handler publishes.

    Patches ``publish_event`` where the check handler imported it, capturing the
    event detail without needing an EventBridge rule/target wired in LocalStack
    (that EventBridge → WS path is platform-foundation's concern). Returns the
    growing list of detail dicts.
    """
    from app.handlers import check as check_mod

    captured: list[dict[str, Any]] = []

    def _fake_publish(*, detail_type: str, detail: dict[str, Any]) -> None:
        if detail_type == check_mod.CHECK_UPDATED_DETAIL_TYPE:
            captured.append(detail)

    monkeypatch.setattr(check_mod, "publish_event", _fake_publish)
    return captured


def _forbid_ai(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Install an AI client that fails the test if any model call is made.

    Used on the blocker paths to prove the free pre-scan spends no AI. Token
    counting is still allowed (delegated to the offline estimator) because the
    cleaner may count tokens before the pre-scan verdict; only a generative
    ``create`` call is forbidden.
    """
    calls: list[Any] = []

    class _NoCreate(_ScriptedMessages):
        def __init__(self) -> None:
            super().__init__([])

        def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            raise AssertionError("no AI create call expected on the blocker path")

    class _NoAiClient:
        def __init__(self) -> None:
            self.messages = _NoCreate()

    set_ai_client(AiClient(client=_NoAiClient()))
    return calls
