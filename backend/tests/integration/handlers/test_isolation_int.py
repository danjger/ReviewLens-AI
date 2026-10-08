"""Isolation integration test for the analysis pipeline (review-analysis task 8).

This proves Requirement 3.1: *during extraction and analysis the Worker uses
only the dataset's stored objects and the AI provider; it does not fetch or
include any other data source.*

Design "Isolation test": "run extraction and analysis with outbound network
blocked, except for the stubbed AI client and LocalStack, to prove no external
data is fetched after capture."

How it works
------------
The one stage that *legitimately* fetches from the public internet is page
collection (via the browser, which lives only in the workers image). Everything
after capture — extraction, dedupe, the entity profile, sentiment, themes,
metrics, and completion — must read only from the dataset's **already-captured**
S3 objects and the **AI provider**, and nothing else.

So this test runs the whole :class:`~app.handlers.processing.ProcessingHandler`
pipeline for a dataset whose pages **and** plan are already in S3, with:

* a **socket-level network guard** installed for the duration of the handler
  call. The guard intercepts every outbound TCP connect and *fails the test* if
  the destination host is anything other than the allowed infrastructure —
  LocalStack (S3 / SQS / EventBridge, reached at ``localhost:4566`` /
  ``*.localstack.cloud``) and the PostgreSQL database host parsed from
  ``DATABASE_URL``. Any attempt to reach a review site, the fixtures host, or
  any other external endpoint raises :class:`OutboundNetworkBlockedError`, which the
  test treats as a failure (proving the pipeline tried to fetch external data);
* the in-process **scripted AI stub** (``set_ai_client``) — the AI client is a
  plain Python object, so AI calls make *no socket connection at all*; the guard
  never even sees them. This matches "except for the stubbed AI client";
* the browser seam ``collection.render_page`` stubbed to **raise** if it is ever
  called. Because every page the pipeline needs is already captured in S3, a
  correct pipeline never renders — so a call here is itself a bug. (Collection
  is the fetch stage; this test is about everything *after* capture.)

If the pipeline is correctly isolated it reaches ``updated`` having touched only
LocalStack, the database, and the in-process AI stub — exactly the allowed set.

Reuse
-----
The dataset/plan/S3 setup, the scripted AI client, and the fixture-page HTML
mirror the task-7 full-pipeline suite
(``test_processing_pipeline_int.py``). They are replicated here (minimally)
rather than imported, because those helpers are module-private to that task's
test file and this task must not modify another task's files.

Running
-------
Run with ``make test-int`` under ``make up`` (needs LocalStack **and** the
Compose PostgreSQL). The module and each test skip cleanly when either is
unavailable, so the suite still *collects* without the stack. The test uses its
own random dataset id and cleans up its database rows and S3 prefix.
"""

from __future__ import annotations

import re
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from app.consumer import MessageMeta
from app.core import db as core_db
from app.core.ai import AiClient, reset_ai_client, set_ai_client
from app.core.config import get_settings
from app.db.models import Base
from app.extraction.models import (
    ExtractionPlan,
    FirstPageStats,
    LocatorSelectors,
    Method,
    NextPageRule,
)
from app.handlers import collection as collection_mod
from app.handlers import processing as processing_mod
from app.storage import keys, s3
from sqlalchemy import text

from tests.support.ai import FakeClaude

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"
_BUS = "reviewlens-events"

#: Fixture pages are addressed on the SSRF-allowed fixtures host, exactly as in
#: the task-7 suite. The host carries a registrable domain
#: (``fixtures.example.com`` → eTLD+1 ``example.com``) so the real ``next_page``
#: engine treats consecutive pages as the same site (Requirement 5.3 /
#: Property 7). In THIS test the pages already live in S3, so the pipeline must
#: never actually resolve or connect to this host — the network guard asserts
#: that (``fixtures.example.com`` is deliberately NOT in the guard's allowlist).
_FIXTURE_HOST = "fixtures.example.com"
_HOST = f"http://{_FIXTURE_HOST}"


def _page_url(page_num: int) -> str:
    return f"{_HOST}/analysis/reviews?page={page_num}"


# ---------------------------------------------------------------------------
# Fixture page HTML (minimal copy of the task-7 fixture page)
# ---------------------------------------------------------------------------


def _review_marker(page_num: int, index: int) -> str:
    """Unique token leading each review body (survives snippet truncation)."""
    return f"P{page_num}R{index}"


def _review_card(page_num: int, index: int, rating: int) -> str:
    marker = _review_marker(page_num, index)
    body = f"{marker}: solid value and the build quality held up well on this one."
    return (
        "<article class='review-card'>"
        f"<p class='review-title'>Review {page_num}-{index}</p>"
        f"<span class='review-rating' aria-label='{rating} out of 5 stars'>{rating}</span>"
        f"<p class='review-body'>{body}</p>"
        f"<span class='review-author'>Reviewer {page_num}-{index}</span>"
        f"<time class='review-date'>2026-0{(index % 9) + 1}-15</time>"
        "</article>"
    )


def _page_html(page_num: int, total: int, *, per_page: int = 3) -> str:
    """Render fixture HTML for *page_num*; links to the next page unless last."""
    if page_num < total:
        nxt = _page_url(page_num + 1)
        pagination = (
            f'<link rel="next" href="{nxt}" />'
            f'<nav class="pagination"><a href="{nxt}">Next \u203a</a></nav>'
        )
        head_link = pagination
    else:
        pagination = '<nav class="pagination"><span class="current">end</span></nav>'
        head_link = ""
    cards = "".join(_review_card(page_num, i, rating=4) for i in range(1, per_page + 1))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Acme Widget reviews \u2014 page {page_num}</title>{head_link}</head>"
        "<body><main><h1>Acme Widget</h1>"
        f"<section class='review-list'>{cards}</section>"
        f"{pagination}</main></body></html>"
    )


#: The plan's review-card selectors, matching the fixture HTML above.
_SELECTORS = LocatorSelectors(
    item=".review-card",
    text=".review-body",
    rating=".review-rating",
    date=".review-date",
    author=".review-author",
    title=".review-title",
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
# The network guard
# ---------------------------------------------------------------------------


class OutboundNetworkBlockedError(AssertionError):
    """Raised when the pipeline tries to connect to a non-allowed host.

    It subclasses ``AssertionError`` so that, were it ever swallowed by the
    pipeline, the test still fails loudly rather than passing silently.
    """


def _allowed_hosts() -> set[str]:
    """Hosts the extraction/analysis stages are permitted to reach.

    Only the backing infrastructure: LocalStack (S3 / SQS / EventBridge) and the
    PostgreSQL database host from ``DATABASE_URL``. Everything else — review
    sites, the fixtures host, metadata endpoints — is blocked.
    """
    allowed: set[str] = {
        "localhost",
        "127.0.0.1",
        "::1",
        "localstack",
    }
    # LocalStack endpoint host (and SQS's localstack.cloud alias is handled by
    # the suffix match in `_is_allowed`).
    endpoint_host = urlparse(_ENDPOINT).hostname
    if endpoint_host:
        allowed.add(endpoint_host)
    # The database host, parsed from the active settings' DATABASE_URL.
    database_url = get_settings().database_url
    if database_url:
        db_host = urlparse(database_url).hostname
        if db_host:
            allowed.add(db_host)
    return allowed


def _is_allowed(host: str) -> bool:
    host = host.lower().strip("[]")  # strip IPv6 brackets
    allowed = _allowed_hosts()
    if host in allowed:
        return True
    # LocalStack's SQS endpoints resolve under *.localstack.cloud.
    return host.endswith(".localstack.cloud") or host == "localstack.cloud"


@contextmanager
def _block_outbound_network(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[str]]:
    """Fail on any TCP connect to a host outside the allowed infrastructure.

    Patches ``socket.socket.connect`` and ``connect_ex`` so every outbound
    connection is checked. Connections to the allowed hosts (LocalStack, the DB)
    pass through to the real implementation; anything else raises
    ``OutboundNetworkBlockedError``. The in-process AI stub makes no socket calls, so
    it is unaffected ("except for the stubbed AI client"). Returns the list of
    allowed destination hosts actually contacted, for a positive assertion that
    only infrastructure was reached.
    """
    contacted: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _host_of(address: Any) -> str | None:
        # AF_INET / AF_INET6 addresses are (host, port[, ...]) tuples; UNIX
        # sockets (used by some local setups) are str paths — always allowed.
        if isinstance(address, (bytes, str)):
            return None
        if isinstance(address, tuple) and address:
            return str(address[0])
        return None

    def _check(address: Any) -> None:
        host = _host_of(address)
        if host is None:
            return  # UNIX socket or unknown shape: not an external fetch.
        if not _is_allowed(host):
            raise OutboundNetworkBlockedError(
                f"extraction/analysis attempted an outbound connection to {host!r}, "
                "which is not LocalStack or the database — Requirement 3.1 forbids "
                "fetching any other data source after capture."
            )
        contacted.append(host)

    def _guarded_connect(self: socket.socket, address: Any) -> None:
        _check(address)
        return real_connect(self, address)

    def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
        _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex, raising=True)
    try:
        yield contacted
    finally:
        monkeypatch.setattr(socket.socket, "connect", real_connect, raising=True)
        monkeypatch.setattr(socket.socket, "connect_ex", real_connect_ex, raising=True)


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
    monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", _FIXTURE_HOST)
    monkeypatch.setenv("PAGE_REQUEST_DELAY_S", "0")
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
def dataset_id() -> Iterator[str]:
    """A dataset id with cleanup of its rows and S3 prefix after the test."""
    did = str(uuid.uuid4())
    try:
        yield did
    finally:
        with core_db.session_scope() as session:
            session.execute(
                text(
                    "DELETE FROM dataset_versions WHERE dataset_id = CAST(:id AS uuid)"
                ).bindparams(id=did)
            )
            session.execute(
                text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)").bindparams(id=did)
            )
        client = s3._get_s3_client()
        listed = client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=keys.dataset_prefix(did))
        for obj in listed.get("Contents", []):
            key = obj.get("Key")
            if key:
                client.delete_object(Bucket=_S3_BUCKET, Key=key)


# ---------------------------------------------------------------------------
# Row + S3 setup helpers
# ---------------------------------------------------------------------------


def _insert_url_dataset(dataset_id: str, *, data_version: int, status: str = "requested") -> None:
    """Insert a URL dataset row and its in-flight version row."""
    with core_db.session_scope() as session:
        session.execute(
            text(
                "INSERT INTO datasets "
                "(id, name, source_type, original_url, final_url, normalized_url, "
                " page_title, status, data_version) "
                "VALUES (CAST(:id AS uuid), :name, 'url', :url, :url, :url, :title, "
                "        CAST(:status AS dataset_status), :dv)"
            ).bindparams(
                id=dataset_id,
                name="Acme Widget reviews",
                url=_page_url(1),
                title="Acme Widget reviews \u2014 page 1",
                status=status,
                dv=data_version,
            )
        )
        session.execute(
            text(
                "INSERT INTO dataset_versions (dataset_id, version, trigger, requested_at) "
                "VALUES (CAST(:id AS uuid), :v, 'add', now())"
            ).bindparams(id=dataset_id, v=data_version)
        )


def _put(key: str, body: str, content_type: str = "text/html; charset=utf-8") -> None:
    s3.put_bytes(key, body.encode("utf-8"), content_type=content_type)


def _save_plan(dataset_id: str, version: int, plan: ExtractionPlan) -> None:
    _put(
        keys.dataset_raw_plan(dataset_id, version),
        plan.model_dump_json(),
        content_type="application/json; charset=utf-8",
    )


def _url_plan(method: Method = "selectors", *, per_page_rate: int = 3) -> ExtractionPlan:
    """Build a plan with the fixture selectors and a no-op next-page rule."""
    return ExtractionPlan(
        created_at="2026-01-01T00:00:00+00:00",
        method=method,
        selectors=_SELECTORS,
        rating_scale=5,
        next_page_rule=NextPageRule(type="none"),
        first_page=FirstPageStats(verified=per_page_rate, per_page_rate=per_page_rate),
    )


def _seed_captured_pages(dataset_id: str, version: int, pages: dict[int, str]) -> None:
    """Write every page HTML to its canonical S3 key (pre-captured)."""
    for page_num, html in pages.items():
        _put(keys.dataset_raw_page(dataset_id, version, page_num), html)


def _install_forbidden_render(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub ``collection.render_page`` to FAIL if ever called.

    All needed pages are pre-captured in S3, so the collection stage must reuse
    them and never render. A call here means the pipeline tried to re-fetch a
    page — a bug this test must surface, distinct from (and stricter than) the
    socket guard. Returns the (expected-empty) list of URLs it was asked for.
    """
    asked: list[str] = []

    def _render(url: str, key: str) -> tuple[str, str]:
        asked.append(url)
        raise AssertionError(
            f"render_page was called for {url!r}; the isolation test pre-captures "
            "every page, so extraction/analysis must not fetch any page."
        )

    monkeypatch.setattr(collection_mod, "render_page", _render)
    return asked


# ---------------------------------------------------------------------------
# Scripted AI client (dispatches by forced-tool name) — in-process, no sockets
# ---------------------------------------------------------------------------


def _tool_use_response(tool_name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    """An SDK-shaped message carrying a single forced ``tool_use`` block."""
    block = SimpleNamespace(type="tool_use", name=tool_name, input=tool_input)
    return SimpleNamespace(
        id="msg_scripted",
        content=[block],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


_MARKER_RE = re.compile(r'"P\d+R\d+:')


class _ScriptedMessages:
    """``messages`` resource: dispatch ``create`` by forced-tool name.

    A plain in-process object: ``create`` and ``count_tokens`` never touch the
    network, so the network guard never sees the AI calls. The Locator response
    is derived from the rendered page lines in the user message so item refs
    resolve against the real DOM (steering: the model never supplies review
    text).
    """

    def __init__(self) -> None:
        self._fake = FakeClaude()
        self.calls: list[dict[str, Any]] = []

    def _tool_name(self, kwargs: dict[str, Any]) -> str:
        tool_choice = kwargs.get("tool_choice") or {}
        name = tool_choice.get("name")
        if name:
            return str(name)
        tools = kwargs.get("tools") or []
        if tools:
            return str(tools[0].get("name", ""))
        return ""

    def _user_text(self, kwargs: dict[str, Any]) -> str:
        for message in kwargs.get("messages", []):
            if message.get("role") == "user":
                content = message.get("content", "")
                if isinstance(content, str):
                    return content
        return ""

    def _n_review_lines(self, user_text: str) -> int:
        count = 0
        for line in user_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and "]" in stripped:
                head = stripped[1 : stripped.index("]")]
                if head.strip().isdigit():
                    count += 1
        return count

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        name = self._tool_name(kwargs)
        user_text = self._user_text(kwargs)

        if name == "locator_result":
            return _tool_use_response("locator_result", _locator_from_user(user_text))

        if name == "entity_profile":
            return _tool_use_response(
                "entity_profile",
                {
                    "name": "Acme Widget",
                    "category": "Gadget",
                    "description": "A widget reviewers discuss.",
                    "confidence": "high",
                },
            )

        if name == "classify_sentiment":
            n = self._n_review_lines(user_text)
            return _tool_use_response(
                "classify_sentiment",
                {"results": [{"id": i, "sentiment": "positive"} for i in range(n)]},
            )

        if name == "extract_themes":
            n = self._n_review_lines(user_text)
            example_ids = [str(i) for i in range(min(n, 3))]
            return _tool_use_response(
                "extract_themes",
                {
                    "themes": [
                        {
                            "label": "Value for money",
                            "mentions": n,
                            "lean": "positive",
                            "example_ids": example_ids,
                        }
                    ]
                },
            )

        raise AssertionError(f"unexpected forced tool: {name!r}")

    def count_tokens(self, **kwargs: Any) -> Any:
        return self._fake.messages.count_tokens(**kwargs)


def _locator_from_user(user_text: str) -> dict[str, Any]:
    """Build a Locator tool input from the cleaned-page user content."""
    items: list[dict[str, Any]] = []
    for line in user_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] != "e":
            continue
        if not _MARKER_RE.search(stripped):
            continue
        ref = stripped.split()[0]
        items.append({"item_ref": ref, "text_ref": ref, "kind": "review"})
    return {
        "has_reviews": bool(items),
        "rating_scale": 5,
        "items": items,
        "selectors": {"item": ".review-card", "text": ".review-body"},
        "next_page": {"ref": None},
        "reported_total": len(items),
        "entity_hint": "Acme Widget",
        "confidence": "high",
    }


class _ScriptedClient:
    """Anthropic-like client whose ``messages`` dispatches by tool name."""

    def __init__(self) -> None:
        self.messages = _ScriptedMessages()


def _install_scripted_ai() -> _ScriptedClient:
    client = _ScriptedClient()
    set_ai_client(AiClient(client=client))
    return client


# ---------------------------------------------------------------------------
# Reading state back
# ---------------------------------------------------------------------------


def _meta(receive_count: int = 1) -> MessageMeta:
    return MessageMeta(message_id="m-iso-int", receive_count=receive_count)


def _row(dataset_id: str) -> dict[str, Any]:
    with core_db.session_scope() as session:
        result = session.execute(
            text(
                "SELECT status, active_version, metrics FROM datasets WHERE id = CAST(:id AS uuid)"
            ).bindparams(id=dataset_id)
        ).first()
    assert result is not None
    status, active_version, metrics = result
    return {
        "status": str(status),
        "active_version": active_version,
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# The isolation test
# ---------------------------------------------------------------------------


def test_extraction_and_analysis_fetch_no_external_data(
    dataset_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction + analysis succeed with outbound network blocked (Req 3.1).

    A `selectors` dataset whose pages and plan are already in S3 runs the whole
    pipeline (extract → dedupe → profile → sentiment → themes → metrics →
    completion). Throughout the handler call, every outbound TCP connection is
    guarded: only LocalStack and the database may be reached. One page is
    deliberately under-yield so it falls back to the Locator, exercising the AI
    path (the in-process stub, which uses no socket) under the guard too.

    The pipeline reaching `updated` while the guard raised nothing proves the
    extraction and analysis stages fetched no external data source — only the
    dataset's stored objects (S3), the database, and the AI provider.

    _Validates: Requirement 3.1_
    """
    _insert_url_dataset(dataset_id, data_version=1)
    # Three pages, all PRE-CAPTURED in S3. Page 2 has a single card
    # (< per_page_rate/2 = 1.5) so it falls back to the Locator (AI path).
    pages = {
        1: _page_html(1, total=3, per_page=3),
        2: _page_html(2, total=3, per_page=1),
        3: _page_html(3, total=3, per_page=3),
    }
    _save_plan(dataset_id, 1, _url_plan("selectors", per_page_rate=3))
    _seed_captured_pages(dataset_id, 1, pages)
    _install_scripted_ai()
    # Collection must not render anything (all pages pre-captured).
    asked = _install_forbidden_render(monkeypatch)

    # Run the pipeline with outbound network locked down to the infrastructure.
    with _block_outbound_network(monkeypatch) as contacted:
        processing_mod.handler.handle({"dataset_id": dataset_id, "version": 1}, _meta())

    # The pipeline completed successfully using only stored objects + the AI.
    row = _row(dataset_id)
    assert row["status"] == "updated"
    assert row["active_version"] == 1
    assert row["metrics"] is not None
    assert row["metrics"]["review_count"] > 0
    # The AI path was exercised (page 2 fell back) yet nothing external was hit.
    assert row["metrics"]["pages_captured"] == 3

    # The browser seam was never used — no page was re-fetched.
    assert asked == []

    # Positive check: the only hosts contacted were allowed infrastructure.
    assert contacted, "expected the pipeline to reach LocalStack/the database"
    assert all(_is_allowed(host) for host in contacted)
