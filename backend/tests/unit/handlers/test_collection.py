"""Unit tests for app.handlers.collection (review-analysis task 2).

These cover every exit of the page-collection loop and its side effects, with
the browser render, the clock, the sleep, S3, the Extraction Engine's
``next_page``, the SSRF check, and the status log all stubbed so no network,
browser, database, or wall-clock wait is involved:

- **no next page** — the engine reports no URL; collection stops at page 1;
- **script-only pagination** — a no-URL control with reason
  ``script_driven_no_url`` records a warning (Requirement 2.7);
- **MAX_PAGES** — collection stops at the configured page cap;
- **MAX_REVIEWS** — the running estimate reaches the cap and collection stops;
- **time budget** — an elapsed budget stops collection before the next fetch;
- **page failure** — a render error stops collection with a warning, keeping
  earlier pages (Requirement 2.3);
- **SSRF block** — a non-public next URL stops collection with a warning
  (Requirement 2.6);
- **already-captured skip** — a page whose object exists is reused, not
  re-rendered (Requirement 7.3);
- **upload skip** — an upload dataset collects nothing (Requirement 2.5);
- **per-host delay** — a same-host fetch waits out the delay; a different host
  does not (Requirement 2.4);
- **progress events** — one event per captured page (Requirement 2.2).

_Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 7.3_
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import get_settings
from app.extraction.models import ExtractionPlan, FirstPageStats, NextPage, NextPageRule
from app.handlers import collection as collection_mod
from app.handlers._timebudget import TimeBudget
from app.ingestion.url_validator import SsrfError

# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------

_FIRST_URL = "https://reviews.example.com/p/1"


class _FakeS3:
    """In-memory stand-in for app.storage.s3 used by the collection stage.

    Backed by a dict of key -> text. ``get_text`` returns stored text,
    ``object_exists`` reports membership, ``put_bytes`` stores decoded bytes.
    """

    def __init__(self, objects: dict[str, str] | None = None) -> None:
        self.objects: dict[str, str] = dict(objects or {})
        self.puts: list[str] = []

    def get_text(self, key: str, *, encoding: str = "utf-8") -> str:
        return self.objects[key]

    def object_exists(self, key: str) -> bool:
        return key in self.objects

    def put_bytes(self, key: str, body: bytes, *, content_type: str) -> None:
        self.objects[key] = body.decode("utf-8")
        self.puts.append(key)


def _plan(verified: int = 10) -> ExtractionPlan:
    """A minimal URL-dataset Extraction Plan with a first-page verified count."""
    return ExtractionPlan(
        created_at="2026-01-01T00:00:00+00:00",
        method="selectors",
        next_page_rule=NextPageRule(type="none"),
        first_page=FirstPageStats(verified=verified),
    )


@pytest.fixture
def events() -> list[tuple[str, dict[str, Any] | None]]:
    """Collects (message, extra) for every log_event call."""
    return []


@pytest.fixture(autouse=True)
def _wire(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, dict[str, Any] | None]],
) -> Iterator[None]:
    """Point the module's S3, status log, SSRF check, clock, and sleep at fakes.

    The browser renderer is left for each test to set via ``monkeypatch`` so a
    test controls exactly what each fetched page returns.
    """
    get_settings.cache_clear()

    def _log_event(dataset_id: str, message: str, extra: dict[str, Any] | None = None) -> None:
        events.append((message, extra))

    monkeypatch.setattr(collection_mod, "log_event", _log_event)
    # Default SSRF check: allow everything. Tests that exercise the block
    # override this.
    monkeypatch.setattr(collection_mod, "assert_public_host", lambda url: None)
    # No real waiting; record requested sleeps instead.
    monkeypatch.setattr(collection_mod, "sleep", lambda _seconds: None)
    yield
    get_settings.cache_clear()


def _fake_s3(monkeypatch: pytest.MonkeyPatch, objects: dict[str, str]) -> _FakeS3:
    fake = _FakeS3(objects)
    monkeypatch.setattr(collection_mod, "s3", fake)
    return fake


def _page1_objects(
    dataset_id: str, version: int, html: str = "<html>page1</html>"
) -> dict[str, str]:
    from app.storage import keys

    return {keys.dataset_raw_page(dataset_id, version, 1): html}


def _stub_next_page(monkeypatch: pytest.MonkeyPatch, results: list[NextPage]) -> list[str]:
    """Make ``extraction_next_page`` return *results* in order; record URLs seen.

    A single trailing result is reused once the list is exhausted so a loop that
    iterates further than expected still gets a deterministic answer.
    """
    seen_urls: list[str] = []
    queue = list(results)

    def _next(html: str, url: str, plan: Any) -> NextPage:
        seen_urls.append(url)
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    monkeypatch.setattr(collection_mod, "extraction_next_page", _next)
    return seen_urls


def _stub_render(
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[str, tuple[str, str]] | None = None,
    *,
    fail_on: str | None = None,
) -> list[tuple[str, str]]:
    """Stub ``render_page`` to return canned (html, final_url) per URL.

    ``pages`` maps a requested URL to the (html, final_url) it yields; an
    unknown URL yields a generic page echoing the URL. ``fail_on`` raises for a
    matching URL to simulate a page that fails to load. Records (url, key) for
    every call so a test can assert what was fetched.
    """
    calls: list[tuple[str, str]] = []
    pages = pages or {}

    def _render(url: str, key: str) -> tuple[str, str]:
        calls.append((url, key))
        if fail_on is not None and url == fail_on:
            raise RuntimeError("render failed")
        if url in pages:
            return pages[url]
        return (f"<html>{url}</html>", url)

    monkeypatch.setattr(collection_mod, "render_page", _render)
    return calls


# ---------------------------------------------------------------------------
# Loop exits
# ---------------------------------------------------------------------------


class TestNoNextPage:
    """Collection stops at page 1 when the engine reports no next page.

    _Validates: Requirement 2.1_
    """

    def test_stops_with_only_first_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_s3(monkeypatch, _page1_objects("ds", 1))
        _stub_next_page(monkeypatch, [NextPage(url=None, rule_used="none")])
        _stub_render(monkeypatch)

        result = collection_mod.collect_pages("ds", 1, _plan(), _FIRST_URL, TimeBudget.start())

        assert result.stop_reason == collection_mod.STOP_NO_NEXT_PAGE
        assert [p.page_num for p in result.pages] == [1]
        assert result.warnings == []


class TestScriptOnlyPagination:
    """A no-URL control with a script-driven reason records a warning.

    _Validates: Requirement 2.7_
    """

    def test_records_script_only_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        events: list[tuple[str, dict[str, Any] | None]],
    ) -> None:
        _fake_s3(monkeypatch, _page1_objects("ds", 1))
        _stub_next_page(
            monkeypatch,
            [NextPage(url=None, rule_used="none", reason_if_none="script_driven_no_url")],
        )
        _stub_render(monkeypatch)

        result = collection_mod.collect_pages("ds", 1, _plan(), _FIRST_URL, TimeBudget.start())

        assert result.stop_reason == collection_mod.STOP_NO_NEXT_PAGE
        assert len(result.warnings) == 1
        assert "script" in result.warnings[0].lower()
        # The warning is also logged as a progress event with the reason.
        assert any(e[1] and e[1].get("reason") == "script_driven_no_url" for e in events)


class TestMaxPages:
    """Collection stops at the configured page cap.

    _Validates: Requirement 2.1_
    """

    def test_stops_at_max_pages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAX_PAGES", "3")
        monkeypatch.setenv("MAX_REVIEWS", "100000")
        get_settings.cache_clear()
        _fake_s3(monkeypatch, _page1_objects("ds", 1))
        # Always another page available.
        _stub_next_page(monkeypatch, [NextPage(url=_FIRST_URL, rule_used="generic_rel_next")])
        _stub_render(monkeypatch)

        result = collection_mod.collect_pages(
            "ds", 1, _plan(verified=1), _FIRST_URL, TimeBudget.start()
        )

        assert result.stop_reason == collection_mod.STOP_MAX_PAGES
        assert [p.page_num for p in result.pages] == [1, 2, 3]


class TestMaxReviews:
    """Collection stops once the running review estimate reaches the cap.

    _Validates: Requirement 2.1_
    """

    def test_stops_at_max_reviews(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAX_PAGES", "10")
        monkeypatch.setenv("MAX_REVIEWS", "25")
        get_settings.cache_clear()
        _fake_s3(monkeypatch, _page1_objects("ds", 1))
        _stub_next_page(monkeypatch, [NextPage(url=_FIRST_URL, rule_used="generic_rel_next")])
        _stub_render(monkeypatch)

        # 10 reviews/page: page1=10, page2=20, page3=30 >= 25 → stop after page 3.
        result = collection_mod.collect_pages(
            "ds", 1, _plan(verified=10), _FIRST_URL, TimeBudget.start()
        )

        assert result.stop_reason == collection_mod.STOP_MAX_REVIEWS
        assert [p.page_num for p in result.pages] == [1, 2, 3]


class TestTimeBudget:
    """An elapsed collection budget stops collection before the next fetch.

    _Validates: Requirement 2.1 (design Error Handling)_
    """

    def test_stops_when_budget_elapsed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        events: list[tuple[str, dict[str, Any] | None]],
    ) -> None:
        _fake_s3(monkeypatch, _page1_objects("ds", 1))
        _stub_next_page(monkeypatch, [NextPage(url=_FIRST_URL, rule_used="generic_rel_next")])
        render_calls = _stub_render(monkeypatch)

        # A budget whose soft collection window is already spent: started far in
        # the past with a tiny total budget.
        import time

        budget = TimeBudget(started_monotonic=time.monotonic() - 10_000, budget_seconds=1.0)
        result = collection_mod.collect_pages("ds", 1, _plan(), _FIRST_URL, budget)

        assert result.stop_reason == collection_mod.STOP_TIME_BUDGET
        assert [p.page_num for p in result.pages] == [1]
        # No page was fetched after page 1.
        assert render_calls == []
        assert len(result.warnings) == 1


class TestPageFailure:
    """A render failure on a later page stops collection but keeps earlier pages.

    _Validates: Requirement 2.3_
    """

    def test_keeps_pages_before_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        events: list[tuple[str, dict[str, Any] | None]],
    ) -> None:
        monkeypatch.setenv("MAX_PAGES", "10")
        monkeypatch.setenv("MAX_REVIEWS", "100000")
        get_settings.cache_clear()
        _fake_s3(monkeypatch, _page1_objects("ds", 1))
        page2 = "https://reviews.example.com/p/2"
        page3 = "https://reviews.example.com/p/3"
        _stub_next_page(
            monkeypatch,
            [
                NextPage(url=page2, rule_used="generic_rel_next"),
                NextPage(url=page3, rule_used="generic_rel_next"),
            ],
        )
        _stub_render(monkeypatch, {page2: ("<html>p2</html>", page2)}, fail_on=page3)

        result = collection_mod.collect_pages(
            "ds", 1, _plan(verified=1), _FIRST_URL, TimeBudget.start()
        )

        assert result.stop_reason == collection_mod.STOP_PAGE_FAILURE
        # Page 1 and the good page 2 are kept; page 3's failure stops the loop.
        assert [p.page_num for p in result.pages] == [1, 2]
        assert len(result.warnings) == 1
        assert "page 3" in result.warnings[0]


class TestSsrfBlock:
    """A non-public next URL stops collection with a warning.

    _Validates: Requirement 2.6_
    """

    def test_blocked_next_url_stops_collection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_s3(monkeypatch, _page1_objects("ds", 1))
        internal = "http://169.254.169.254/latest/"
        _stub_next_page(monkeypatch, [NextPage(url=internal, rule_used="generic_rel_next")])
        render_calls = _stub_render(monkeypatch)

        def _block(url: str) -> None:
            raise SsrfError("Address not allowed")

        monkeypatch.setattr(collection_mod, "assert_public_host", _block)

        result = collection_mod.collect_pages("ds", 1, _plan(), _FIRST_URL, TimeBudget.start())

        assert result.stop_reason == collection_mod.STOP_PAGE_FAILURE
        assert [p.page_num for p in result.pages] == [1]
        # The blocked URL is never fetched.
        assert render_calls == []
        assert len(result.warnings) == 1


class TestAlreadyCaptured:
    """A page whose object already exists is reused, not re-rendered.

    _Validates: Requirement 7.3_
    """

    def test_reuses_existing_page_without_rendering(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.storage import keys

        monkeypatch.setenv("MAX_PAGES", "3")
        monkeypatch.setenv("MAX_REVIEWS", "100000")
        get_settings.cache_clear()
        page2_key = keys.dataset_raw_page("ds", 1, 2)
        page2_url = "https://reviews.example.com/p/2"
        page3_url = "https://reviews.example.com/p/3"
        # Page 1 and page 2 already captured (a retry after a crash); page 3 is
        # new and must be rendered.
        objects = _page1_objects("ds", 1)
        objects[page2_key] = "<html>cached p2</html>"
        _fake_s3(monkeypatch, objects)
        _stub_next_page(
            monkeypatch,
            [
                NextPage(url=page2_url, rule_used="generic_rel_next"),
                NextPage(url=page3_url, rule_used="generic_rel_next"),
            ],
        )
        render_calls = _stub_render(monkeypatch)

        result = collection_mod.collect_pages(
            "ds", 1, _plan(verified=1), _FIRST_URL, TimeBudget.start()
        )

        page2 = next(p for p in result.pages if p.page_num == 2)
        assert page2.reused is True
        assert page2.html == "<html>cached p2</html>"
        rendered_urls = [url for url, _ in render_calls]
        # Page 2 was reused (never rendered); page 3 was rendered.
        assert page2_url not in rendered_urls
        assert page3_url in rendered_urls


class TestUploadSkip:
    """Upload datasets skip collection entirely.

    _Validates: Requirement 2.5_
    """

    def test_upload_collects_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No S3 or next_page interaction should happen for an upload.
        fake = _fake_s3(monkeypatch, {})
        result = collection_mod.collect_pages("ds", 1, None, "", TimeBudget.start(), is_upload=True)
        assert result.stop_reason == collection_mod.STOP_SKIPPED_UPLOAD
        assert result.pages == []
        assert fake.puts == []


class TestProgressEvents:
    """One progress event is appended per captured page.

    _Validates: Requirement 2.2_
    """

    def test_progress_event_per_captured_page(
        self,
        monkeypatch: pytest.MonkeyPatch,
        events: list[tuple[str, dict[str, Any] | None]],
    ) -> None:
        monkeypatch.setenv("MAX_PAGES", "3")
        monkeypatch.setenv("MAX_REVIEWS", "100000")
        get_settings.cache_clear()
        _fake_s3(monkeypatch, _page1_objects("ds", 1))
        _stub_next_page(monkeypatch, [NextPage(url=_FIRST_URL, rule_used="generic_rel_next")])
        _stub_render(monkeypatch)

        collection_mod.collect_pages("ds", 1, _plan(verified=1), _FIRST_URL, TimeBudget.start())

        captured = [m for m, _ in events if m.startswith("Captured page")]
        # Pages 2 and 3 captured (page 1 came from the Check); each logs once.
        assert captured == [
            "Captured page 2 of up to 3",
            "Captured page 3 of up to 3",
        ]


class TestPerHostDelay:
    """The per-host delay is applied for same-host fetches, skipped otherwise.

    _Validates: Requirement 2.4_
    """

    def test_same_host_waits_other_host_does_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAX_PAGES", "3")
        monkeypatch.setenv("MAX_REVIEWS", "100000")
        monkeypatch.setenv("PAGE_REQUEST_DELAY_S", "1.0")
        get_settings.cache_clear()
        _fake_s3(monkeypatch, _page1_objects("ds", 1))
        same_host = "https://reviews.example.com/p/2"
        other_host = "https://other.example.net/p/3"
        _stub_next_page(
            monkeypatch,
            [
                NextPage(url=same_host, rule_used="generic_rel_next"),
                NextPage(url=other_host, rule_used="generic_rel_next"),
            ],
        )
        _stub_render(
            monkeypatch,
            {
                same_host: ("<html>p2</html>", same_host),
                other_host: ("<html>p3</html>", other_host),
            },
        )

        slept: list[float] = []
        monkeypatch.setattr(collection_mod, "sleep", lambda s: slept.append(s))
        # Freeze the clock so the computed remaining delay is the full delay.
        monkeypatch.setattr(collection_mod, "monotonic", lambda: 1000.0)

        collection_mod.collect_pages("ds", 1, _plan(verified=1), _FIRST_URL, TimeBudget.start())

        # Page 2 is the same registrable host as page 1 → one full-delay sleep.
        # Page 3 is a different host → no sleep.
        assert slept == [1.0]
