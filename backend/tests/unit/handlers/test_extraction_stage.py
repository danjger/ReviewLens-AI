"""Unit tests for app.handlers.extraction_stage (review-analysis task 3.1).

These cover the per-page extraction stage: it calls the Extraction Engine's
``extract_page`` for each captured page with the saved plan, passes
``is_last=True`` only for the final page, and collects the combined reviews
(tagged with their source page, in page order) plus the per-page details
(method used, reviews found, fallback, discards).

The Extraction Engine is stubbed so no AI, browser, or network is involved;
the stage is pure over its ``CapturedPage`` inputs and the plan.

_Validates: Requirements 3.1, 3.2_
"""

from __future__ import annotations

from typing import Any

import pytest
from app.extraction.errors import AIUnavailable, LocatorUnavailable
from app.extraction.models import (
    ExtractionPlan,
    FirstPageStats,
    NextPage,
    NextPageRule,
    PageResult,
    VerifiedReview,
)
from app.handlers import extraction_stage
from app.handlers.collection import CapturedPage

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _plan(method: str = "selectors") -> ExtractionPlan:
    return ExtractionPlan(
        created_at="2026-01-01T00:00:00+00:00",
        method=method,  # type: ignore[arg-type]
        next_page_rule=NextPageRule(type="none"),
        first_page=FirstPageStats(verified=10, per_page_rate=10),
    )


def _page(num: int, url: str | None = None, html: str | None = None) -> CapturedPage:
    return CapturedPage(
        page_num=num,
        url=url or f"https://reviews.example.com/p/{num}",
        html=html or f"<html>page{num}</html>",
        key=f"datasets/ds/raw/v1/page-{num}.html",
        reused=num == 1,
    )


def _review(text: str) -> VerifiedReview:
    return VerifiedReview(text=text)


def _page_result(
    reviews: list[VerifiedReview],
    *,
    method_used: str = "selectors",
    fallback: bool = False,
    discarded: dict[str, int] | None = None,
) -> PageResult:
    return PageResult(
        reviews=reviews,
        method_used=method_used,  # type: ignore[arg-type]
        fallback=fallback,
        discarded=discarded or {},
        next_page=NextPage(url=None, rule_used="none"),
    )


def _stub_extract(
    monkeypatch: pytest.MonkeyPatch, results: list[PageResult]
) -> list[tuple[str, str, ExtractionPlan, bool]]:
    """Make the stage's ``extraction_extract_page`` return *results* in order.

    Records (html, url, plan, is_last) for every call so a test can assert what
    was passed, including that ``is_last`` is set only for the final page.
    """
    calls: list[tuple[str, str, ExtractionPlan, bool]] = []
    queue = list(results)

    def _extract(html: str, url: str, plan: ExtractionPlan, *, is_last: bool) -> PageResult:
        calls.append((html, url, plan, is_last))
        return queue.pop(0)

    monkeypatch.setattr(extraction_stage, "extraction_extract_page", _extract)
    return calls


# ---------------------------------------------------------------------------
# Review collection across pages
# ---------------------------------------------------------------------------


class TestCollectsReviews:
    """Reviews from every page are combined in page order, tagged by page.

    _Validates: Requirement 3.2_
    """

    def test_combines_reviews_in_page_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pages = [_page(1), _page(2)]
        _stub_extract(
            monkeypatch,
            [
                _page_result([_review("a1"), _review("a2")]),
                _page_result([_review("b1")]),
            ],
        )

        result = extraction_stage.extract_pages(pages, _plan())

        assert [c.review.text for c in result.reviews] == ["a1", "a2", "b1"]
        assert [c.source_page for c in result.reviews] == [1, 1, 2]

    def test_single_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_extract(monkeypatch, [_page_result([_review("only")])])

        result = extraction_stage.extract_pages([_page(1)], _plan())

        assert [c.review.text for c in result.reviews] == ["only"]
        assert [c.source_page for c in result.reviews] == [1]

    def test_empty_pages_yields_empty_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Defensive: no captured pages → nothing extracted, no engine calls.
        calls = _stub_extract(monkeypatch, [])

        result = extraction_stage.extract_pages([], _plan())

        assert result.reviews == []
        assert result.pages == []
        assert calls == []


# ---------------------------------------------------------------------------
# is_last handling
# ---------------------------------------------------------------------------


class TestIsLast:
    """``is_last`` is True only for the final page (suppresses low-yield fallback).

    _Validates: Requirement 3.2_
    """

    def test_is_last_only_on_final_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pages = [_page(1), _page(2), _page(3)]
        calls = _stub_extract(
            monkeypatch,
            [_page_result([]), _page_result([]), _page_result([])],
        )

        extraction_stage.extract_pages(pages, _plan())

        assert [is_last for (_h, _u, _p, is_last) in calls] == [False, False, True]

    def test_single_page_is_last(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _stub_extract(monkeypatch, [_page_result([])])

        extraction_stage.extract_pages([_page(1)], _plan())

        assert calls[0][3] is True


class TestPassesPlanAndContent:
    """Each page's own HTML and URL and the saved plan reach the engine.

    _Validates: Requirements 3.1, 3.2_
    """

    def test_passes_page_html_url_and_plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = _plan()
        pages = [
            _page(1, url="https://x/1", html="<p>one</p>"),
            _page(2, url="https://x/2", html="<p>two</p>"),
        ]
        calls = _stub_extract(monkeypatch, [_page_result([]), _page_result([])])

        extraction_stage.extract_pages(pages, plan)

        assert calls[0][0] == "<p>one</p>"
        assert calls[0][1] == "https://x/1"
        assert calls[0][2] is plan
        assert calls[1][0] == "<p>two</p>"
        assert calls[1][1] == "https://x/2"
        assert calls[1][2] is plan


# ---------------------------------------------------------------------------
# Per-page details
# ---------------------------------------------------------------------------


class TestPageDetails:
    """Per-page details record method, found, fallback, and discards.

    _Validates: Requirement 3.2_
    """

    def test_records_method_found_and_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pages = [_page(1), _page(2)]
        _stub_extract(
            monkeypatch,
            [
                _page_result(
                    [_review("a"), _review("b")],
                    method_used="selectors",
                    fallback=False,
                ),
                _page_result(
                    [_review("c")],
                    method_used="ai_direct",
                    fallback=True,
                ),
            ],
        )

        result = extraction_stage.extract_pages(pages, _plan())

        first, second = result.pages
        assert (first.page, first.url, first.method, first.found, first.fallback) == (
            1,
            "https://reviews.example.com/p/1",
            "selectors",
            2,
            False,
        )
        assert (second.page, second.method, second.found, second.fallback) == (
            2,
            "ai_direct",
            1,
            True,
        )

    def test_discarded_sums_reason_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The engine reports per-reason discard counts; the detail is their sum.
        _stub_extract(
            monkeypatch,
            [
                _page_result(
                    [_review("a")],
                    discarded={"too_short": 2, "duplicate": 3, "structured_unverified": 1},
                )
            ],
        )

        result = extraction_stage.extract_pages([_page(1)], _plan())

        assert result.pages[0].discarded == 6

    def test_discarded_excludes_structured_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``structured_added`` is an addition recorded in the same dict, not a
        # discard; it must not inflate the discard count.
        _stub_extract(
            monkeypatch,
            [
                _page_result(
                    [_review("a")],
                    discarded={"duplicate": 2, "structured_added": 5},
                )
            ],
        )

        result = extraction_stage.extract_pages([_page(1)], _plan())

        assert result.pages[0].discarded == 2

    def test_no_discards_is_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_extract(monkeypatch, [_page_result([_review("a")], discarded={})])

        result = extraction_stage.extract_pages([_page(1)], _plan())

        assert result.pages[0].discarded == 0

    def test_one_detail_per_page_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pages = [_page(1), _page(2), _page(3)]
        _stub_extract(
            monkeypatch,
            [_page_result([]), _page_result([]), _page_result([])],
        )

        result = extraction_stage.extract_pages(pages, _plan())

        assert [d.page for d in result.pages] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Error propagation (retryable engine errors are not swallowed)
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    """``AIUnavailable`` / ``LocatorUnavailable`` propagate to the pipeline.

    _Validates: Requirement 3.1 (design Error Handling: let SQS retry)_
    """

    def test_ai_unavailable_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_a: Any, **_k: Any) -> PageResult:
            raise AIUnavailable("provider down")

        monkeypatch.setattr(extraction_stage, "extraction_extract_page", _raise)

        with pytest.raises(AIUnavailable):
            extraction_stage.extract_pages([_page(1)], _plan())

    def test_locator_unavailable_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_a: Any, **_k: Any) -> PageResult:
            raise LocatorUnavailable("bad response")

        monkeypatch.setattr(extraction_stage, "extraction_extract_page", _raise)

        with pytest.raises(LocatorUnavailable):
            extraction_stage.extract_pages([_page(1)], _plan())
