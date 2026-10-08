"""Integration tests for the page-collection stage (review-analysis task 2).

These drive :func:`app.handlers.collection.collect_pages` against the **real**
backing services the stage touches — the LocalStack S3 bucket (page reads,
existence checks, writes) and the Compose PostgreSQL database (the dataset row
and its append-only ``status_detail`` event log written by
:func:`app.db.status.log_event`) — with a multi-page fixture whose pages
paginate by ``rel="next"`` / ``?page=`` on the SSRF-allowed ``fixtures`` host.

The real :func:`app.extraction.next_page` engine resolves each next page from
the stored HTML, the real :func:`app.ingestion.url_validator.assert_public_host`
SSRF check runs against the allowed ``fixtures`` host, and every S3 key comes
from :mod:`app.storage.keys`. Only the browser render is stubbed — it writes the
next fixture page's HTML to the canonical S3 key, standing in for Chromium
(which lives only in the workers image) — so the storage, pagination, SSRF, and
status-log integration is exercised end to end.

Covered:

* the happy path collects every page the fixture links to, in order, writing
  each to ``raw/v{n}/page-{k}.html`` and appending one "Captured page …"
  progress event per page (Requirements 2.1, 2.2);
* a retry after a crash reuses pages already captured for the version rather
  than re-rendering them (Requirement 7.3).

Run with ``make test-int``. The module skips cleanly when LocalStack or
PostgreSQL is not reachable, matching the other integration suites. Each test
uses its own random dataset id and cleans up its S3 prefix and database rows.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import httpx
import pytest
from app.core import db as core_db
from app.core.config import get_settings
from app.db.models import Base
from app.extraction.models import ExtractionPlan, FirstPageStats, NextPageRule
from app.handlers import collection as collection_mod
from app.handlers._timebudget import TimeBudget
from app.storage import keys, s3
from sqlalchemy import text

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"
_BUS = "reviewlens-events"

# A tiny three-page fixture that paginates on the SSRF-allowed fixtures host.
# Each page links to the next with a `rel="next"` + a labelled "Next" anchor so
# the real `next_page` engine resolves it; the last page links nowhere.
#
# The host MUST have a registrable domain (eTLD+1) so the real `next_page`
# engine treats page N and page N+1 as the same site (Requirement 5.3 /
# Property 7, computed via `tldextract`). A bare single-label host like
# `fixtures` has no registrable domain, so every next-page candidate is dropped
# and collection stops at page 1. `fixtures.example.com` resolves to the
# registrable domain `example.com`, so pagination advances. Only `next_page`
# resolution and the SSRF gate use this host here (render is stubbed below), so
# no real HTTP ever reaches it and no Compose DNS alias is required.
_FIXTURE_HOST = "fixtures.example.com"
_HOST = f"http://{_FIXTURE_HOST}"
_PAGE_URLS = [
    f"{_HOST}/collection/reviews?page=1",
    f"{_HOST}/collection/reviews?page=2",
    f"{_HOST}/collection/reviews?page=3",
]


def _page_html(page_num: int, total: int) -> str:
    """Render fixture HTML for *page_num*; links to the next page unless last."""
    if page_num < total:
        nxt = _PAGE_URLS[page_num]  # page N links to page N+1 (0-based index N)
        pagination = (
            f'<link rel="next" href="{nxt}" />'
            f'<nav class="pagination"><a href="{nxt}">Next ›</a></nav>'
        )
    else:
        pagination = '<nav class="pagination"><span class="current">end</span></nav>'
    head_link = pagination if page_num < total else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Reviews page {page_num}</title>{head_link}</head>"
        "<body><main><section class='review-list'>"
        "<article class='review-card'>"
        f"<p class='review-body'>Review on page {page_num}.</p></article>"
        f"</section>{pagination}</main></body></html>"
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
    """Point AWS clients at LocalStack, allow the fixtures host for SSRF."""
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
    """A URL dataset row; remove its rows and S3 prefix afterwards."""
    did = str(uuid.uuid4())
    engine = core_db.get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO datasets (id, name, source_type, final_url, status, data_version) "
                "VALUES (CAST(:id AS uuid), :name, 'url', :final_url, 'processing', 1)"
            ).bindparams(id=did, name="Collection IT", final_url=_PAGE_URLS[0])
        )
    try:
        yield did
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)").bindparams(id=did)
            )
        client = s3._get_s3_client()
        listed = client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=keys.dataset_prefix(did))
        for obj in listed.get("Contents", []):
            key = obj.get("Key")
            if key:
                client.delete_object(Bucket=_S3_BUCKET, Key=key)


def _plan() -> ExtractionPlan:
    return ExtractionPlan(
        created_at="2026-01-01T00:00:00+00:00",
        method="selectors",
        next_page_rule=NextPageRule(type="none"),
        first_page=FirstPageStats(verified=1),
    )


def _install_fixture_render(monkeypatch: pytest.MonkeyPatch, total: int) -> list[str]:
    """Stub render_page to write the matching fixture page to the canonical key.

    Stands in for the browser: given the URL and the canonical S3 key, it writes
    the correct page's HTML to S3 (real LocalStack write) and returns (html,
    url). Records the URLs rendered so a test can assert what was fetched.
    """
    rendered: list[str] = []

    def _render(url: str, key: str) -> tuple[str, str]:
        rendered.append(url)
        page_num = _PAGE_URLS.index(url) + 1
        html = _page_html(page_num, total)
        s3.put_bytes(key, html.encode("utf-8"), content_type="text/html; charset=utf-8")
        return html, url

    monkeypatch.setattr(collection_mod, "render_page", _render)
    return rendered


def _events(dataset_id: str) -> list[dict[str, object]]:
    engine = core_db.get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT status_detail FROM datasets WHERE id = CAST(:id AS uuid)").bindparams(
                id=dataset_id
            )
        ).first()
    assert row is not None
    detail = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    events: list[dict[str, object]] = detail.get("events", [])
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_collects_every_linked_page(dataset_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path walks the fixture to its last page via the real engine.

    _Validates: Requirements 2.1, 2.2, 2.6_
    """
    total = len(_PAGE_URLS)
    # Page 1 is written by Add/Refresh before processing; seed it in S3.
    s3.put_bytes(
        keys.dataset_raw_page(dataset_id, 1, 1),
        _page_html(1, total).encode("utf-8"),
        content_type="text/html; charset=utf-8",
    )
    rendered = _install_fixture_render(monkeypatch, total)

    result = collection_mod.collect_pages(dataset_id, 1, _plan(), _PAGE_URLS[0], TimeBudget.start())

    # All three pages collected in order; stops when the last page links nowhere.
    assert [p.page_num for p in result.pages] == [1, 2, 3]
    assert result.stop_reason == collection_mod.STOP_NO_NEXT_PAGE
    # Pages 2 and 3 were fetched (page 1 came from the Check).
    assert rendered == [_PAGE_URLS[1], _PAGE_URLS[2]]
    # Each fetched page is persisted at its canonical key.
    for k in (2, 3):
        assert s3.object_exists(keys.dataset_raw_page(dataset_id, 1, k))
    # One progress event per captured page (Requirement 2.2).
    messages = [e.get("message") for e in _events(dataset_id)]
    assert "Captured page 2 of up to 10" in messages
    assert "Captured page 3 of up to 10" in messages


def test_retry_reuses_already_captured_pages(
    dataset_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry after a crash reuses captured pages rather than re-rendering.

    _Validates: Requirement 7.3_
    """
    total = len(_PAGE_URLS)
    # Simulate a prior attempt that captured pages 1 and 2 already.
    for k in (1, 2):
        s3.put_bytes(
            keys.dataset_raw_page(dataset_id, 1, k),
            _page_html(k, total).encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )
    rendered = _install_fixture_render(monkeypatch, total)

    result = collection_mod.collect_pages(dataset_id, 1, _plan(), _PAGE_URLS[0], TimeBudget.start())

    assert [p.page_num for p in result.pages] == [1, 2, 3]
    # Page 2 was reused (already present); only page 3 was rendered.
    reused = {p.page_num: p.reused for p in result.pages}
    assert reused[2] is True
    assert reused[3] is False
    assert rendered == [_PAGE_URLS[2]]
