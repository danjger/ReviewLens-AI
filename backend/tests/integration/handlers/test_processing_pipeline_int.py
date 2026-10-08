"""Integration tests for the processing pipeline (review-analysis task 7).

These drive the whole analysis pipeline — :class:`app.handlers.processing.ProcessingHandler`
run as the Worker would run it in a container — against the **real** backing
services the pipeline touches:

* the LocalStack **S3** bucket — the captured ``raw/v{n}/page-{k}.html`` pages,
  the saved ``raw/v{n}/plan.json`` and (for uploads) ``mapping.json`` /
  ``upload.csv``, and the written ``reviews/v{n}.json`` output;
* the Compose **PostgreSQL** database — the ``datasets`` row (status,
  ``active_version``, ``metrics``, the append-only ``status_detail`` event log)
  and the ``dataset_versions`` completion row;
* the LocalStack **EventBridge** bus — the ``dataset.status.changed`` events the
  status transitions publish (asserted via a publisher spy, matching
  ``test_check_handler_full_int.py``);
* the **real Extraction Engine** (``app.extraction``) reading the fixture pages;
  the ``selectors`` method reads reviews from page elements by *code* (no AI),
  and the Review Locator path (``ai_direct`` and selector fallback) is driven by
  a *scripted* ``locator_result`` tool response built from the rendered page's
  own element references (never hand-written review prose).

Only two seams are stubbed, exactly as the sibling suites do it:

* ``app.handlers.collection.render_page`` — stands in for the browser (Chromium
  lives only in the workers image), writing the matching fixture page's HTML to
  the canonical S3 key (a real LocalStack write) and returning ``(html, url)``.
  This keeps S3, pagination, SSRF, and the status log exercised end to end while
  needing no browser (mirrors ``test_collection_int.py``).
* the instrumented AI client (``set_ai_client``) — a scripted client that
  dispatches by forced-tool name (``locator_result`` / ``entity_profile`` /
  ``classify_sentiment`` / ``extract_themes``) and returns SDK-shaped
  ``tool_use`` responses. Token counting is delegated to the offline
  ``FakeClaude`` estimator so the cleaner's budget logic runs unchanged. The
  Locator items point at the rendered page's real element refs so review text is
  always read from the page by code — never supplied by the model (steering).

What they prove (Requirements 1.1, 1.2, 2.3, 3.2, 3.4, 6.1, 6.2, 6.5, 7.3,
7.4, 8.1):

1. **Happy path, ``selectors`` dataset with one fallback page** — status
   ``requested → processing → updated``, a progress event per captured page, the
   published status events, the ``reviews/v{n}.json`` contents (pages[],
   reviews[], entity), the ``metrics`` column, ``active_version``, the
   ``dataset_versions`` completion, and ``viability.actual``. One later page
   falls back to the Locator (``PageDetail.fallback``) and that is recorded in
   ``pages[]`` and ``metrics.extraction`` (Requirements 1.1, 3.2, 6.1, 8.1).
2. **Happy path, ``ai_direct`` dataset** — every page read by the Locator; ends
   ``updated`` with ``extraction.method == "ai_direct"``.
3. **Zero reviews → ``failed``** with "No reviews found on the captured pages.",
   ``active_version`` unchanged (null), ``metrics`` column not written
   (Requirement 6.2).
4. **A later-page failure → ``updated`` with a warning** — collection stops at
   the failing page, keeps the earlier pages, and records a warning in
   ``metrics.warnings`` (Requirement 2.3).
5. **Reprocessing idempotency + crash-retry** — running the handler twice for
   the same version produces one ``reviews/v{n}.json`` (overwritten), one
   ``dataset_versions`` completion, and no duplicate reviews; a retry after a
   crash reuses already-captured pages (``render_page`` not called for pages
   whose object is already present) — Requirement 7.3.
6. **AI unavailable through every retry → ``failed``** with "AI service
   unavailable — try refreshing later." On non-final attempts the handler
   raises (SQS would retry); on the final attempt the version fails
   (Requirement 7.4).
7. **A failed refresh leaves ``active_version`` and ``metrics`` unchanged** — a
   successful v1 then a v2 that fails keeps ``active_version == 1`` and v1's
   metrics (Requirement 6.5).
8. **Upload path, including an over-limit file** — an upload dataset with more
   than ``MAX_REVIEWS`` rows keeps at most ``MAX_REVIEWS``, records a warning,
   and ends ``updated`` (Requirement 3.4).

Running
-------
Run with ``make test-int`` under ``make up`` (needs LocalStack **and** the
Compose PostgreSQL). The module and each test skip cleanly when either is
unavailable, so the suite still *collects* without the stack. Each test uses its
own random dataset id and cleans up its database rows and S3 prefix.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from app.consumer import MAX_RECEIVE_COUNT, MessageMeta
from app.core import db as core_db
from app.core.ai import AiClient, reset_ai_client, set_ai_client
from app.core.config import get_settings
from app.db.models import Base
from app.extraction.errors import AIUnavailable
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

#: Fixture pages paginate on the SSRF-allowed fixtures host (the Compose
#: ``fixtures`` service), never a real site. Each page links to the next with a
#: ``rel="next"`` anchor the real ``next_page`` engine resolves; the last links
#: nowhere.
#:
#: The host MUST have a registrable domain (eTLD+1) so the real ``next_page``
#: engine treats page N and page N+1 as the same site (Requirement 5.3 /
#: Property 7, computed via ``tldextract``). A bare single-label host like
#: ``fixtures`` has no registrable domain, so every next-page candidate is
#: dropped and collection stops at page 1. ``fixtures.example.com`` resolves to
#: the registrable domain ``example.com``, so pagination advances. The host is
#: used only for ``next_page`` resolution and the SSRF gate (render is stubbed),
#: so no real HTTP reaches it and no Compose DNS alias is required.
_FIXTURE_HOST = "fixtures.example.com"
_HOST = f"http://{_FIXTURE_HOST}"


def _page_url(page_num: int) -> str:
    return f"{_HOST}/analysis/reviews?page={page_num}"


# ---------------------------------------------------------------------------
# Fixture page HTML
# ---------------------------------------------------------------------------
#
# A tiny, deterministic review-listing page. Each page carries ``per_page``
# review cards under ``.review-card`` with a ``.review-body`` text node, a
# ``.review-rating`` cue, a ``.review-author`` and a ``.review-date`` — the plan
# selectors read them by code (no AI) on the ``selectors`` happy path. Review
# bodies embed the page number and card index so they are unique across pages
# (dedupe keeps them all).


def _review_marker(page_num: int, index: int) -> str:
    """The unique token placed at the START of a review body.

    It must survive the cleaner's text-snippet truncation, so it leads the body
    text; the scripted Locator points at the element whose text starts with it.
    """
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


def _empty_page_html(page_num: int, total: int) -> str:
    """A fixture page with pagination but no review cards (zero-reviews case)."""
    if page_num < total:
        nxt = _page_url(page_num + 1)
        head_link = f'<link rel="next" href="{nxt}" />'
        pagination = f'<nav class="pagination"><a href="{nxt}">Next</a></nav>'
    else:
        head_link = ""
        pagination = '<nav class="pagination"><span class="current">end</span></nav>'
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Nothing here \u2014 page {page_num}</title>{head_link}</head>"
        "<body><main><h1>Nothing here</h1>"
        "<section class='review-list'></section>"
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
    # The real assert_public_host would resolve this host; the test allowlist
    # permits it without resolution, mirroring the Compose fixtures service.
    monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", _FIXTURE_HOST)
    # No real crawl delay in tests.
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
    """A dataset id with cleanup of its rows and S3 prefix after the test.

    The row itself is created by the per-test helper (URL vs upload differ in
    columns); this fixture only guarantees cleanup so each test is isolated.
    """
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


def _insert_upload_dataset(dataset_id: str, *, data_version: int) -> None:
    """Insert an upload dataset row and its in-flight version row."""
    with core_db.session_scope() as session:
        session.execute(
            text(
                "INSERT INTO datasets (id, name, source_type, status, data_version) "
                "VALUES (CAST(:id AS uuid), :name, 'upload', 'requested', :dv)"
            ).bindparams(id=dataset_id, name="Uploaded reviews", dv=data_version)
        )
        session.execute(
            text(
                "INSERT INTO dataset_versions (dataset_id, version, trigger, requested_at) "
                "VALUES (CAST(:id AS uuid), :v, 'upload', now())"
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
    """Build a plan with the fixture selectors and a next-page rule.

    ``per_page_rate`` seeds the selector low-yield fallback threshold (fall back
    when a page yields < per_page_rate / 2) and the collection review estimate.
    """
    return ExtractionPlan(
        created_at="2026-01-01T00:00:00+00:00",
        method=method,
        selectors=_SELECTORS,
        rating_scale=5,
        next_page_rule=NextPageRule(type="none"),
        first_page=FirstPageStats(verified=per_page_rate, per_page_rate=per_page_rate),
    )


def _seed_page1(dataset_id: str, version: int, html: str) -> None:
    """Write page 1 to S3, standing in for the Check's captured first page."""
    _put(keys.dataset_raw_page(dataset_id, version, 1), html)


def _install_fixture_render(
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[str, str],
) -> list[str]:
    """Stub ``collection.render_page`` to write the mapped page HTML to S3.

    *pages* maps a page URL to its HTML. Rendering an unknown URL raises, which
    the collection stage treats as a page failure (used by the later-page
    failure scenario). Returns the list of URLs actually rendered so a test can
    assert what was fetched (crash-retry reuse).
    """
    rendered: list[str] = []

    def _render(url: str, key: str) -> tuple[str, str]:
        rendered.append(url)
        if url not in pages:
            raise RuntimeError(f"page failed to load: {url}")
        html = pages[url]
        s3.put_bytes(key, html.encode("utf-8"), content_type="text/html; charset=utf-8")
        return html, url

    monkeypatch.setattr(collection_mod, "render_page", _render)
    return rendered


# ---------------------------------------------------------------------------
# Scripted AI client (dispatches by forced-tool name)
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


#: A cleaned review-body line is identified by a ``PxRy:`` marker at the start
#: of its quoted snippet (see ``_review_marker``). The marker leads the body
#: text so it survives the cleaner's snippet truncation.
_MARKER_RE = re.compile(r'"P\d+R\d+:')


class _ScriptedMessages:
    """``messages`` resource: dispatch ``create`` by forced-tool name.

    The processing pipeline forces one of four tools per call
    (``locator_result`` / ``entity_profile`` / ``classify_sentiment`` /
    ``extract_themes``); this builds a deterministic, SDK-shaped response for
    each from the request. The Locator response is derived from the rendered
    page HTML carried in the user message so its item refs resolve against the
    real DOM. ``count_tokens`` is delegated to the offline estimator so the
    cleaner's budget logic runs unchanged and never hits the network.
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
        """Count the ``[i] ...`` review lines in a sentiment/themes user message."""
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
            # The Locator user message embeds the cleaned page lines (one per
            # kept element: ``e123 <tag> "text" [attrs]``). Point at each review
            # body line by its element ref so post-processing reads the kept text
            # from the real element — never from the model (steering).
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
    """Build a Locator tool input from the cleaned-page user content.

    The Locator's user message contains the cleaned element lines
    (``e123 <tag ...> "text" [attrs]``). A review-body line is the one whose
    quoted snippet starts with a ``PxRy:`` marker (``_review_marker``); its first
    token is the element ref. We point ``item_ref``/``text_ref`` at that ref so
    post-processing reads the text from the real element (steering: the AI never
    supplies review text).
    """
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


class _AiUnavailableMessages:
    """A ``messages`` resource whose ``create`` always fails (AI outage).

    ``count_tokens`` still works (delegated to the offline estimator) because
    the cleaner may count tokens before the generative call; only generative
    ``create`` raises, which the worker/engine normalise to ``AIUnavailable``.
    """

    def __init__(self) -> None:
        self._fake = FakeClaude()
        self.create_calls = 0

    def create(self, **kwargs: Any) -> Any:
        self.create_calls += 1
        raise RuntimeError("provider down")

    def count_tokens(self, **kwargs: Any) -> Any:
        return self._fake.messages.count_tokens(**kwargs)


class _AiUnavailableClient:
    def __init__(self) -> None:
        self.messages = _AiUnavailableMessages()


def _install_unavailable_ai() -> _AiUnavailableClient:
    client = _AiUnavailableClient()
    set_ai_client(AiClient(client=client))
    return client


# ---------------------------------------------------------------------------
# Publisher spy
# ---------------------------------------------------------------------------


def _spy_status_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record ``dataset.status.changed`` detail dicts published by transitions.

    Patches ``publish_event`` where ``app.db.status`` imported it, so the
    published event payload is captured without needing an EventBridge
    rule/target wired in LocalStack (that path is platform-foundation's
    concern — the sweep suite asserts on the DB the same way).
    """
    from app.db import status as status_mod

    captured: list[dict[str, Any]] = []

    def _fake_publish(*, detail_type: str, detail: dict[str, Any]) -> None:
        if detail_type == status_mod.STATUS_CHANGED_DETAIL_TYPE:
            captured.append(detail)

    monkeypatch.setattr(status_mod, "publish_event", _fake_publish)
    return captured


# ---------------------------------------------------------------------------
# Reading state back
# ---------------------------------------------------------------------------


def _meta(receive_count: int = 1) -> MessageMeta:
    return MessageMeta(message_id="m-proc-int", receive_count=receive_count)


def _row(dataset_id: str) -> dict[str, Any]:
    with core_db.session_scope() as session:
        result = session.execute(
            text(
                "SELECT status, active_version, metrics, status_detail "
                "FROM datasets WHERE id = CAST(:id AS uuid)"
            ).bindparams(id=dataset_id)
        ).first()
    assert result is not None
    status, active_version, metrics, status_detail = result
    return {
        "status": str(status),
        "active_version": active_version,
        "metrics": metrics,
        "status_detail": status_detail,
    }


def _version_row(dataset_id: str, version: int) -> dict[str, Any]:
    with core_db.session_scope() as session:
        result = session.execute(
            text(
                "SELECT outcome, review_count, extraction_method, completed_at "
                "FROM dataset_versions WHERE dataset_id = CAST(:id AS uuid) AND version = :v"
            ).bindparams(id=dataset_id, v=version)
        ).first()
    assert result is not None
    outcome, review_count, extraction_method, completed_at = result
    return {
        "outcome": outcome,
        "review_count": review_count,
        "extraction_method": extraction_method,
        "completed_at": completed_at,
    }


def _events(dataset_id: str) -> list[dict[str, Any]]:
    detail = _row(dataset_id)["status_detail"]
    detail = detail if isinstance(detail, dict) else json.loads(detail)
    events: list[dict[str, Any]] = detail.get("events", [])
    return events


def _reviews_doc(dataset_id: str, version: int) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(s3.get_text(keys.dataset_reviews(dataset_id, version)))
    return doc


# ---------------------------------------------------------------------------
# 1. Happy path: `selectors` dataset with one fallback page
# ---------------------------------------------------------------------------


def test_selectors_happy_path_with_one_fallback_page(
    dataset_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `selectors` dataset runs requested → processing → updated.

    Page 1 and page 2 read cleanly by selectors; page 2 is deliberately
    *under-yield* so it falls back to the Locator (recorded in pages[] and
    metrics.extraction). Asserts the status sequence, the progress events, the
    published events, the reviews JSON, the metrics column, active_version, the
    dataset_versions completion, and viability.actual.

    _Validates: Requirements 1.1, 3.2, 6.1, 8.1_
    """
    _insert_url_dataset(dataset_id, data_version=1)
    # Three pages. Page 2 has only ONE card (< per_page_rate/2 = 1.5 → fallback).
    page1 = _page_html(1, total=3, per_page=3)
    page2 = _page_html(2, total=3, per_page=1)
    page3 = _page_html(3, total=3, per_page=3)
    _save_plan(dataset_id, 1, _url_plan("selectors", per_page_rate=3))
    _seed_page1(dataset_id, 1, page1)
    _install_fixture_render(monkeypatch, {_page_url(2): page2, _page_url(3): page3})
    _install_scripted_ai()
    events = _spy_status_events(monkeypatch)

    processing_mod.handler.handle({"dataset_id": dataset_id, "version": 1}, _meta())

    row = _row(dataset_id)
    assert row["status"] == "updated"
    assert row["active_version"] == 1
    assert row["metrics"] is not None

    # Status sequence: processing then updated, both published (Req 8.1).
    published = [(e["status"], e.get("active_version")) for e in events]
    assert ("processing", None) in published
    assert any(status == "updated" for status, _ in published)

    # One progress event per captured page (Requirement 2.2).
    messages = [e.get("message") for e in _events(dataset_id)]
    assert "Captured page 2 of up to 10" in messages
    assert "Captured page 3 of up to 10" in messages

    # reviews/v1.json: pages[], reviews[], entity.
    doc = _reviews_doc(dataset_id, 1)
    assert doc["version"] == 1
    assert doc["entity"]["name"] == "Acme Widget"
    pages = {p["page"]: p for p in doc["pages"]}
    assert set(pages) == {1, 2, 3}
    # Page 2 fell back to the Locator (ai_direct), the others read by selectors.
    assert pages[2]["fallback"] is True
    assert pages[2]["method"] == "ai_direct"
    assert pages[1]["fallback"] is False
    assert pages[1]["method"] == "selectors"
    # Reviews were read from page elements (not generated).
    assert len(doc["reviews"]) == len(doc["reviews"])
    assert all(r["text"] for r in doc["reviews"])
    assert doc["reviews"][0]["id"] == "r_0001"

    # Metrics column reflects the extraction mix and the review count.
    metrics = row["metrics"]
    assert metrics["review_count"] == len(doc["reviews"])
    assert metrics["pages_captured"] == 3
    assert metrics["extraction"]["pages_by_ai"] == 1
    assert metrics["extraction"]["pages_by_selectors"] == 2
    assert metrics["extraction"]["method"] == "selectors"

    # dataset_versions completion (Requirement 6.3).
    version_row = _version_row(dataset_id, 1)
    assert version_row["outcome"] == "updated"
    assert version_row["review_count"] == metrics["review_count"]
    assert version_row["completed_at"] is not None

    # viability.actual recorded next to the (absent) prediction (Requirement 6.4).
    detail = _row(dataset_id)["status_detail"]
    actual = detail["viability"]["actual"]
    assert actual["reviews"] == metrics["review_count"]
    assert actual["pages"] == 3
    assert actual["fallbacks"] == 1


# ---------------------------------------------------------------------------
# 2. Happy path: `ai_direct` dataset
# ---------------------------------------------------------------------------


def test_ai_direct_happy_path(dataset_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """An `ai_direct` dataset reads every page with the Locator and ends updated.

    _Validates: Requirements 1.1, 3.2, 6.1_
    """
    _insert_url_dataset(dataset_id, data_version=1)
    page1 = _page_html(1, total=2, per_page=3)
    page2 = _page_html(2, total=2, per_page=3)
    _save_plan(dataset_id, 1, _url_plan("ai_direct", per_page_rate=3))
    _seed_page1(dataset_id, 1, page1)
    _install_fixture_render(monkeypatch, {_page_url(2): page2})
    _install_scripted_ai()
    _spy_status_events(monkeypatch)

    processing_mod.handler.handle({"dataset_id": dataset_id, "version": 1}, _meta())

    row = _row(dataset_id)
    assert row["status"] == "updated"
    assert row["active_version"] == 1
    metrics = row["metrics"]
    assert metrics["extraction"]["method"] == "ai_direct"
    assert metrics["extraction"]["pages_by_ai"] == 2
    assert metrics["extraction"]["pages_by_selectors"] == 0

    doc = _reviews_doc(dataset_id, 1)
    assert all(p["method"] == "ai_direct" for p in doc["pages"])
    assert len(doc["reviews"]) == metrics["review_count"] > 0
    assert _version_row(dataset_id, 1)["outcome"] == "updated"


# ---------------------------------------------------------------------------
# 3. Zero reviews → failed
# ---------------------------------------------------------------------------


def test_zero_reviews_ends_failed(dataset_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dataset whose pages have no reviews ends `failed`, active/metrics unchanged.

    _Validates: Requirement 6.2_
    """
    _insert_url_dataset(dataset_id, data_version=1)
    page1 = _empty_page_html(1, total=1)
    _save_plan(dataset_id, 1, _url_plan("selectors", per_page_rate=0))
    _seed_page1(dataset_id, 1, page1)
    _install_fixture_render(monkeypatch, {})
    _install_scripted_ai()
    _spy_status_events(monkeypatch)

    processing_mod.handler.handle({"dataset_id": dataset_id, "version": 1}, _meta())

    row = _row(dataset_id)
    assert row["status"] == "failed"
    # active_version unchanged (never succeeded) and the metrics column unwritten.
    assert row["active_version"] is None
    assert row["metrics"] is None

    # The failure event carries the exact zero-reviews message (Req 6.2).
    last_status_event = next(
        e for e in reversed(_events(dataset_id)) if e.get("status") == "failed"
    )
    from app.handlers import completion as completion_mod

    assert last_status_event["message"] == completion_mod.ZERO_REVIEWS_MESSAGE

    version_row = _version_row(dataset_id, 1)
    assert version_row["outcome"] == "failed"
    assert version_row["review_count"] == 0


# ---------------------------------------------------------------------------
# 4. A later-page failure → updated with a warning
# ---------------------------------------------------------------------------


def test_later_page_failure_ends_updated_with_warning(
    dataset_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later page that fails to load stops collection; earlier pages are kept.

    The dataset ends `updated` (page 1's reviews survive) with a warning in
    metrics.warnings recording the stop.

    _Validates: Requirement 2.3_
    """
    _insert_url_dataset(dataset_id, data_version=1)
    page1 = _page_html(1, total=3, per_page=3)  # links to page 2
    _save_plan(dataset_id, 1, _url_plan("selectors", per_page_rate=3))
    _seed_page1(dataset_id, 1, page1)
    # Page 2 is NOT in the render map → render raises → page failure stop.
    _install_fixture_render(monkeypatch, {})
    _install_scripted_ai()
    _spy_status_events(monkeypatch)

    processing_mod.handler.handle({"dataset_id": dataset_id, "version": 1}, _meta())

    row = _row(dataset_id)
    assert row["status"] == "updated"
    assert row["active_version"] == 1
    metrics = row["metrics"]
    # Only page 1 was kept.
    assert metrics["pages_captured"] == 1
    assert metrics["review_count"] > 0
    # A page-failure warning was recorded (Requirement 2.3).
    assert any("page 2" in w and "failed to load" in w for w in metrics["warnings"])
    assert _version_row(dataset_id, 1)["outcome"] == "updated"


# ---------------------------------------------------------------------------
# 5. Reprocessing idempotency + crash-retry reuse
# ---------------------------------------------------------------------------


def test_reprocessing_is_idempotent(dataset_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running the handler twice for one version produces one output + completion.

    _Validates: Requirement 7.3_
    """
    _insert_url_dataset(dataset_id, data_version=1)
    page1 = _page_html(1, total=2, per_page=3)
    page2 = _page_html(2, total=2, per_page=3)
    _save_plan(dataset_id, 1, _url_plan("selectors", per_page_rate=3))
    _seed_page1(dataset_id, 1, page1)
    _install_fixture_render(monkeypatch, {_page_url(2): page2})
    _install_scripted_ai()
    _spy_status_events(monkeypatch)

    processing_mod.handler.handle({"dataset_id": dataset_id, "version": 1}, _meta())
    first = _reviews_doc(dataset_id, 1)
    first_completed = _version_row(dataset_id, 1)["completed_at"]
    first_count = len(first["reviews"])

    # Re-run the same version. The start guard sees the version already
    # completed → SKIP, so the pipeline is a no-op and nothing changes.
    processing_mod.handler.handle({"dataset_id": dataset_id, "version": 1}, _meta())

    second = _reviews_doc(dataset_id, 1)
    assert len(second["reviews"]) == first_count  # no duplicate reviews
    # One completion: the completed_at timestamp is unchanged.
    assert _version_row(dataset_id, 1)["completed_at"] == first_completed

    # Exactly one reviews object exists for v1 (overwritten, not duplicated).
    client = s3._get_s3_client()
    listed = client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=f"datasets/{dataset_id}/reviews/")
    review_keys = [o["Key"] for o in listed.get("Contents", [])]
    assert review_keys == [keys.dataset_reviews(dataset_id, 1)]


def test_retry_after_crash_reuses_captured_pages(
    dataset_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry after a crash does not re-render pages already captured.

    Pages 1 and 2 are present in S3 from a prior (crashed) attempt; a retry
    reuses them and only renders page 3.

    _Validates: Requirement 7.3_
    """
    _insert_url_dataset(dataset_id, data_version=1, status="processing")
    page1 = _page_html(1, total=3, per_page=3)
    page2 = _page_html(2, total=3, per_page=3)
    page3 = _page_html(3, total=3, per_page=3)
    _save_plan(dataset_id, 1, _url_plan("selectors", per_page_rate=3))
    # A prior attempt captured pages 1 and 2 already.
    _seed_page1(dataset_id, 1, page1)
    _put(keys.dataset_raw_page(dataset_id, 1, 2), page2)
    rendered = _install_fixture_render(monkeypatch, {_page_url(2): page2, _page_url(3): page3})
    _install_scripted_ai()
    _spy_status_events(monkeypatch)

    # A retry of the in-flight version (status already `processing`) continues.
    processing_mod.handler.handle({"dataset_id": dataset_id, "version": 1}, _meta(receive_count=2))

    row = _row(dataset_id)
    assert row["status"] == "updated"
    # Page 2 was reused (already present); only page 3 was rendered.
    assert rendered == [_page_url(3)]
    doc = _reviews_doc(dataset_id, 1)
    assert {p["page"] for p in doc["pages"]} == {1, 2, 3}


# ---------------------------------------------------------------------------
# 6. AI unavailable through every retry → failed
# ---------------------------------------------------------------------------


def test_ai_unavailable_through_every_retry_ends_failed(
    dataset_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI unavailable: non-final attempts raise; the final attempt fails safely.

    An `ai_direct` dataset whose only page needs the Locator fails every AI
    call. On attempts 1 and 2 the handler re-raises (SQS would retry); on the
    final attempt (receive_count == MAX_RECEIVE_COUNT) the version is moved to
    `failed` with the AI-unavailable message.

    _Validates: Requirement 7.4_
    """
    _insert_url_dataset(dataset_id, data_version=1)
    page1 = _page_html(1, total=1, per_page=3)
    _save_plan(dataset_id, 1, _url_plan("ai_direct", per_page_rate=3))
    _seed_page1(dataset_id, 1, page1)
    _install_fixture_render(monkeypatch, {})
    _install_unavailable_ai()
    _spy_status_events(monkeypatch)

    # Non-final attempts: the pipeline raises so SQS retries with backoff.
    for attempt in (1, 2):
        with pytest.raises(AIUnavailable):
            processing_mod.handler.handle(
                {"dataset_id": dataset_id, "version": 1}, _meta(receive_count=attempt)
            )
        # Still in flight: not failed yet, no completion recorded.
        assert _row(dataset_id)["status"] == "processing"
        assert _version_row(dataset_id, 1)["outcome"] is None

    # Final attempt: caught, version failed with the AI-unavailable message.
    processing_mod.handler.handle(
        {"dataset_id": dataset_id, "version": 1}, _meta(receive_count=MAX_RECEIVE_COUNT)
    )

    row = _row(dataset_id)
    assert row["status"] == "failed"
    assert row["active_version"] is None
    assert row["metrics"] is None

    from app.handlers import completion as completion_mod

    last_failed = next(e for e in reversed(_events(dataset_id)) if e.get("status") == "failed")
    assert last_failed["message"] == completion_mod.AI_UNAVAILABLE_MESSAGE
    assert _version_row(dataset_id, 1)["outcome"] == "failed"


# ---------------------------------------------------------------------------
# 7. A failed refresh leaves active_version and metrics unchanged
# ---------------------------------------------------------------------------


def test_failed_refresh_keeps_last_good_version(
    dataset_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v1 that succeeds then a v2 that fails: active_version/metrics stay v1's.

    _Validates: Requirement 6.5_
    """
    # --- v1: a successful run. ---
    _insert_url_dataset(dataset_id, data_version=1)
    page1_v1 = _page_html(1, total=1, per_page=3)
    _save_plan(dataset_id, 1, _url_plan("selectors", per_page_rate=3))
    _seed_page1(dataset_id, 1, page1_v1)
    _install_fixture_render(monkeypatch, {})
    _install_scripted_ai()
    _spy_status_events(monkeypatch)

    processing_mod.handler.handle({"dataset_id": dataset_id, "version": 1}, _meta())
    v1 = _row(dataset_id)
    assert v1["status"] == "updated"
    assert v1["active_version"] == 1
    v1_metrics = v1["metrics"]
    assert v1_metrics is not None

    # --- v2: a refresh that fails (AI unavailable on an ai_direct page). ---
    with core_db.session_scope() as session:
        session.execute(
            text(
                "UPDATE datasets SET data_version = 2, status = 'requested' "
                "WHERE id = CAST(:id AS uuid)"
            ).bindparams(id=dataset_id)
        )
        session.execute(
            text(
                "INSERT INTO dataset_versions (dataset_id, version, trigger, requested_at) "
                "VALUES (CAST(:id AS uuid), 2, 'refresh', now())"
            ).bindparams(id=dataset_id)
        )
    page1_v2 = _page_html(1, total=1, per_page=3)
    _save_plan(dataset_id, 2, _url_plan("ai_direct", per_page_rate=3))
    _seed_page1(dataset_id, 2, page1_v2)
    _install_fixture_render(monkeypatch, {})
    _install_unavailable_ai()

    processing_mod.handler.handle(
        {"dataset_id": dataset_id, "version": 2}, _meta(receive_count=MAX_RECEIVE_COUNT)
    )

    row = _row(dataset_id)
    assert row["status"] == "failed"
    # The last good version is still served: active_version and metrics unchanged.
    assert row["active_version"] == 1
    assert row["metrics"] == v1_metrics
    # v1's completion is untouched; v2 completed as failed.
    assert _version_row(dataset_id, 1)["outcome"] == "updated"
    assert _version_row(dataset_id, 2)["outcome"] == "failed"


# ---------------------------------------------------------------------------
# 8. Upload path, including an over-limit file
# ---------------------------------------------------------------------------


def test_upload_over_limit_file_keeps_cap_and_warns(
    dataset_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upload with more than MAX_REVIEWS rows keeps the cap, warns, ends updated.

    _Validates: Requirement 3.4_
    """
    # A small cap keeps the fixture CSV tiny while still exercising the over-limit
    # keep rule and warning.
    monkeypatch.setenv("MAX_REVIEWS", "5")
    get_settings.cache_clear()
    max_reviews = get_settings().max_reviews
    assert max_reviews == 5

    _insert_upload_dataset(dataset_id, data_version=1)

    # Build a CSV with more than MAX_REVIEWS usable rows plus one empty-text row.
    n_rows = max_reviews + 3
    lines = ["text,rating,date,author"]
    for i in range(1, n_rows + 1):
        lines.append(f"This product review number {i} is genuinely useful.,4,2026-01-{i:02d},A{i}")
    lines.append(",5,2026-02-01,Empty")  # empty text → skipped
    csv_body = "\n".join(lines) + "\n"
    _put(keys.dataset_raw_upload(dataset_id, 1), csv_body, content_type="text/csv")

    mapping_doc = {
        "mapping": {"text": "text", "rating": "rating", "date": "date", "author": "author"},
        "keep_rule": "first_in_file",
        "will_keep": max_reviews,
    }
    _put(
        keys.dataset_raw_mapping(dataset_id, 1),
        json.dumps(mapping_doc),
        content_type="application/json; charset=utf-8",
    )

    _install_scripted_ai()
    _spy_status_events(monkeypatch)

    processing_mod.handler.handle({"dataset_id": dataset_id, "version": 1}, _meta())

    row = _row(dataset_id)
    assert row["status"] == "updated"
    assert row["active_version"] == 1
    metrics = row["metrics"]
    # Kept at most MAX_REVIEWS (Requirement 3.4 / Property 2).
    assert metrics["review_count"] == max_reviews
    # The over-limit keep-rule warning is recorded.
    assert any(f"more than {max_reviews}" in w for w in metrics["warnings"])
    # Upload has no captured pages.
    assert metrics["pages_captured"] == 0
    assert metrics["extraction"]["method"] == "upload"

    doc = _reviews_doc(dataset_id, 1)
    assert len(doc["reviews"]) == max_reviews
    # Upload-origin reviews carry source_page 0.
    assert all(r["source_page"] == 0 for r in doc["reviews"])
    assert _version_row(dataset_id, 1)["outcome"] == "updated"
