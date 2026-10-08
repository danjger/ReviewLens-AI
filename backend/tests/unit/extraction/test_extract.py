"""Unit tests for per-page extraction (``app.extraction.extract``), Task 7.

Covers :func:`app.extraction.extract_page` and the selector-extraction helper,
exercising **every branch** of Requirement 6:

- ``selectors`` method, good yield → selector extraction, no fallback, reviews
  read by code, next page resolved;
- ``selectors`` method, low yield (``< per_page_rate / 2``) and not last →
  Locator fallback, ``fallback=True``, ``method_used="ai_direct"``;
- ``selectors`` method, low yield but **last** page → no fallback;
- selectors that raise → zero yield → fallback when not last;
- ``ai_direct`` method → the Locator path;
- ``structured`` method with structured data → structured reviews;
- ``structured`` method with no structured data → Locator fallback;
- structured cross-check: a structured review the chosen method missed is added
  and ``structured_agreement`` is reported;
- ``PageResult`` completeness on a representative case.

The AI is stubbed with the inline ``tool_use`` pattern from ``test_plan.py`` /
``test_locator.py``; the Locator path hits the rate limiter, so the DynamoDB
rate-limit table is created with moto ``mock_aws``.
"""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.ai import AiClient, reset_ai_client, set_ai_client
from app.core.config import get_settings
from app.extraction import extract
from app.extraction.extract import extract_by_selectors, extract_page
from app.extraction.models import (
    ExtractionPlan,
    FirstPageStats,
    LocatorSelectors,
    NextPageRule,
    PageResult,
)
from moto import mock_aws

from tests.support.dynamodb import ensure_rate_limit_table

_TABLE = "rate-limits"
_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# AWS / AI stub plumbing (mirrors test_plan.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    reset_ai_client()


def _create_rate_limit_table() -> None:
    """Create the ``rate-limits`` table (idempotent, shared schema)."""
    ensure_rate_limit_table(_TABLE, region=_REGION)


def _count_tokens(**kwargs: Any) -> SimpleNamespace:  # noqa: ANN401
    """Stub token counter: a small count keeps every test page under budget."""
    return SimpleNamespace(input_tokens=100)


def _tool_use_response(tool_input: dict[str, Any]) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name="locator_result", input=tool_input)
    return SimpleNamespace(
        id="msg_test",
        content=[block],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class _ScriptedClient:
    """Anthropic-like client returning queued responses in order."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create, count_tokens=_count_tokens)

    def _create(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("Scripted client ran out of responses")
        return self._responses.pop(0)


def _install(client: Any) -> None:  # noqa: ANN401
    set_ai_client(AiClient(client=client))


# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

_BODIES = [
    "Great tool, the setup took an afternoon but it worked well overall here.",
    "Support was responsive and fixed my issue within a single business day.",
    "The dashboard is intuitive and the reports export cleanly to CSV files.",
    "Pricing is fair for a small team and the onboarding was quite painless.",
    "Occasional slowness at peak hours but reliable the rest of the time too.",
]


def _cards(count: int, *, with_rating: bool = False) -> str:
    cards = []
    for i in range(count):
        rating = (
            f'<span class="stars" aria-label="{(i % 5) + 1} out of 5 stars"></span>'
            if with_rating
            else ""
        )
        cards.append(
            '<article class="review-card">'
            f'<p class="body">{_BODIES[i]}</p>'
            f'<span class="author">Reviewer {i}</span>'
            f'<time class="date">2024-01-0{i + 1}</time>'
            f"{rating}"
            "</article>"
        )
    return "".join(cards)


def _reviews_html(count: int, *, with_rating: bool = False, with_next: bool = True) -> str:
    next_link = '<a class="next" rel="next" href="/reviews?page=2">Next</a>' if with_next else ""
    return (
        "<html><body><main><div class='reviews'>"
        f"{_cards(count, with_rating=with_rating)}"
        f"</div>{next_link}</main></body></html>"
    )


def _structured_block(count: int, review_count: int | None = None) -> str:
    import json

    rc = review_count if review_count is not None else count
    payload = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Acme CRM",
        "aggregateRating": {"@type": "AggregateRating", "reviewCount": rc},
        "review": [
            {
                "@type": "Review",
                "reviewBody": _BODIES[i],
                "author": {"@type": "Person", "name": f"R{i}"},
            }
            for i in range(count)
        ],
    }
    return '<script type="application/ld+json">' + json.dumps(payload) + "</script>"


def _html_with_structured(card_count: int, structured_count: int, *, with_next: bool = True) -> str:
    next_link = '<a class="next" rel="next" href="/reviews?page=2">Next</a>' if with_next else ""
    return (
        "<html><body>"
        f"{_structured_block(structured_count)}"
        "<main><div class='reviews'>"
        f"{_cards(card_count)}"
        f"</div>{next_link}</main></body></html>"
    )


# ---------------------------------------------------------------------------
# Plan builders
# ---------------------------------------------------------------------------


def _selectors_plan(
    *,
    per_page_rate: int = 5,
    rating: str | None = None,
    item: str = "article.review-card",
    rating_scale: float | None = 5,
) -> ExtractionPlan:
    return ExtractionPlan(
        version=1,
        created_at="2024-01-01T00:00:00+00:00",
        locator_model="model-x",
        prompt_version="locator_v1",
        method="selectors",
        selectors=LocatorSelectors(
            item=item,
            text="p.body",
            author="span.author",
            date="time.date",
            rating=rating,
        ),
        rating_scale=rating_scale,
        next_page_rule=NextPageRule(type="selector", css="a.next"),
        first_page=FirstPageStats(verified=per_page_rate, per_page_rate=per_page_rate),
    )


def _ai_direct_plan() -> ExtractionPlan:
    return ExtractionPlan(
        version=1,
        created_at="2024-01-01T00:00:00+00:00",
        method="ai_direct",
        selectors=LocatorSelectors(),
        rating_scale=5,
        next_page_rule=NextPageRule(type="none"),
        first_page=FirstPageStats(verified=5, per_page_rate=5),
    )


def _structured_plan(per_page_rate: int = 5) -> ExtractionPlan:
    return ExtractionPlan(
        version=1,
        created_at="2024-01-01T00:00:00+00:00",
        method="structured",
        selectors=LocatorSelectors(),
        rating_scale=5,
        next_page_rule=NextPageRule(type="none"),
        first_page=FirstPageStats(verified=per_page_rate, per_page_rate=per_page_rate),
    )


def _ref_for_body(html: str, body_index: int) -> str:
    from app.extraction import cleaner

    result = cleaner.build_clean_result(html)
    target = _BODIES[body_index][:40]
    for line in result.lines:
        if "<p>" in line and target in line:
            return line.strip().split(" ", 1)[0]
    raise AssertionError(f"No ref found for body {body_index}")


def _locator_items(html: str, count: int) -> list[dict[str, Any]]:
    return [
        {"item_ref": _ref_for_body(html, i), "text_ref": _ref_for_body(html, i), "kind": "review"}
        for i in range(count)
    ]


def _locator_input(html: str, count: int, **overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    base: dict[str, Any] = {
        "has_reviews": True,
        "rating_scale": 5,
        "items": _locator_items(html, count),
        "selectors": {"item": "article.review-card", "text": "p.body"},
        "next_page": {"ref": None},
        "reported_total": 99,
        "entity_hint": "Acme CRM",
        "confidence": "high",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# selectors method — good yield (no AI call needed)
# ---------------------------------------------------------------------------


class TestSelectorsGoodYield:
    @mock_aws
    def test_selector_extraction_no_fallback_reads_by_code(self) -> None:
        _create_rate_limit_table()
        # No AI responses queued: a good-yield selector page must not call the AI.
        _install(_ScriptedClient([]))
        html = _reviews_html(5)
        plan = _selectors_plan(per_page_rate=5)

        result = extract_page(html, "https://example.com/reviews?page=3", plan, is_last=False)

        assert result.method_used == "selectors"
        assert result.fallback is False
        assert len(result.reviews) == 5
        # Text was read from the page elements by code (never invented).
        assert result.reviews[0].text == _BODIES[0]
        assert result.reviews[0].author == "Reviewer 0"
        # Date parsed to ISO by the shared postprocess helper.
        assert result.reviews[0].date == "2024-01-01"
        # Next page resolved (generic rel=next on the page).
        assert result.next_page.url is not None
        # No structured data → no agreement reported, no blocker.
        assert result.structured_agreement is None
        assert result.blocker is None

    @mock_aws
    def test_selector_rating_kept_only_with_cue_in_range(self) -> None:
        _create_rate_limit_table()
        _install(_ScriptedClient([]))
        html = _reviews_html(5, with_rating=True)
        plan = _selectors_plan(per_page_rate=5, rating="span.stars", rating_scale=5)

        result = extract_page(html, "https://example.com/reviews", plan, is_last=True)

        ratings = [r.rating for r in result.reviews]
        # aria-label "N out of 5 stars" → N, all within 1..5.
        assert ratings == [1.0, 2.0, 3.0, 4.0, 5.0]


# ---------------------------------------------------------------------------
# selectors method — low yield fallback (Requirement 6.1)
# ---------------------------------------------------------------------------


class TestSelectorsLowYieldFallback:
    @mock_aws
    def test_low_yield_not_last_falls_back_to_locator(self) -> None:
        _create_rate_limit_table()
        # First page saw 5/page; this page has only 1 card (< 5/2) → fallback.
        html = _reviews_html(1)
        _install(_ScriptedClient([_tool_use_response(_locator_input(html, 1))]))
        plan = _selectors_plan(per_page_rate=5)

        result = extract_page(html, "https://example.com/reviews", plan, is_last=False)

        assert result.fallback is True
        assert result.method_used == "ai_direct"
        assert len(result.reviews) == 1
        # The Locator ran, so its reported total surfaces.
        assert result.reported_total == 99

    @mock_aws
    def test_low_yield_last_page_no_fallback(self) -> None:
        _create_rate_limit_table()
        _install(_ScriptedClient([]))  # No AI call on the last page.
        html = _reviews_html(1)
        plan = _selectors_plan(per_page_rate=5)

        result = extract_page(html, "https://example.com/reviews", plan, is_last=True)

        assert result.fallback is False
        assert result.method_used == "selectors"
        assert len(result.reviews) == 1

    @mock_aws
    def test_selectors_that_throw_are_zero_yield_and_fall_back(self) -> None:
        _create_rate_limit_table()
        html = _reviews_html(5)
        _install(_ScriptedClient([_tool_use_response(_locator_input(html, 5))]))
        # An invalid CSS selector makes selectolax raise → zero yield.
        plan = _selectors_plan(per_page_rate=5, item="article::::bad")

        result = extract_page(html, "https://example.com/reviews", plan, is_last=False)

        assert result.fallback is True
        assert result.method_used == "ai_direct"
        assert len(result.reviews) == 5

    @mock_aws
    def test_selectors_that_throw_on_last_page_yield_zero(self) -> None:
        _create_rate_limit_table()
        _install(_ScriptedClient([]))
        html = _reviews_html(5)
        plan = _selectors_plan(per_page_rate=5, item="article::::bad")

        result = extract_page(html, "https://example.com/reviews", plan, is_last=True)

        assert result.fallback is False
        assert result.method_used == "selectors"
        assert result.reviews == []

    @mock_aws
    def test_unknown_per_page_rate_never_falls_back(self) -> None:
        _create_rate_limit_table()
        _install(_ScriptedClient([]))
        html = _reviews_html(1)
        # per_page_rate 0 means "no baseline" → never fall back even on zero.
        plan = _selectors_plan(per_page_rate=0)

        result = extract_page(html, "https://example.com/reviews", plan, is_last=False)

        assert result.fallback is False
        assert result.method_used == "selectors"


# ---------------------------------------------------------------------------
# ai_direct method (Requirement 6.2)
# ---------------------------------------------------------------------------


class TestAiDirectMethod:
    @mock_aws
    def test_ai_direct_runs_locator_path(self) -> None:
        _create_rate_limit_table()
        html = _reviews_html(5)
        _install(_ScriptedClient([_tool_use_response(_locator_input(html, 5))]))
        plan = _ai_direct_plan()

        result = extract_page(html, "https://example.com/reviews", plan, is_last=False)

        assert result.method_used == "ai_direct"
        assert result.fallback is False
        assert len(result.reviews) == 5
        assert result.reported_total == 99


# ---------------------------------------------------------------------------
# structured method (Requirement 6.3)
# ---------------------------------------------------------------------------


class TestStructuredMethod:
    @mock_aws
    def test_structured_with_data_uses_structured(self) -> None:
        _create_rate_limit_table()
        _install(_ScriptedClient([]))  # No AI call when structured data exists.
        html = _html_with_structured(card_count=5, structured_count=5)
        plan = _structured_plan()

        result = extract_page(html, "https://example.com/reviews", plan, is_last=False)

        assert result.method_used == "structured"
        assert result.fallback is False
        assert len(result.reviews) == 5
        # AggregateRating.reviewCount surfaces as reported_total.
        assert result.reported_total == 5
        # Structured data present → agreement reported (perfect self-agreement).
        assert result.structured_agreement == 1.0

    @mock_aws
    def test_structured_without_data_falls_back_to_locator(self) -> None:
        _create_rate_limit_table()
        html = _reviews_html(5)  # No structured data at all.
        _install(_ScriptedClient([_tool_use_response(_locator_input(html, 5))]))
        plan = _structured_plan()

        result = extract_page(html, "https://example.com/reviews", plan, is_last=False)

        assert result.fallback is True
        assert result.method_used == "ai_direct"
        assert len(result.reviews) == 5
        assert result.structured_agreement is None


# ---------------------------------------------------------------------------
# Structured cross-check (Requirement 6.4)
# ---------------------------------------------------------------------------


class TestStructuredCrossCheck:
    @mock_aws
    def test_missed_structured_review_is_added_and_agreement_reported(self) -> None:
        _create_rate_limit_table()
        _install(_ScriptedClient([]))  # selectors good-yield, no AI.
        # 5 cards on the page, but structured data holds all 5 too; make the
        # selector miss one card by giving it a selector that only matches 4.
        html = _html_with_structured(card_count=5, structured_count=5)
        # Narrow the item selector to the first four cards via :nth-child filter.
        plan = _selectors_plan(per_page_rate=4, item="article.review-card:nth-child(-n+4)")

        result = extract_page(html, "https://example.com/reviews", plan, is_last=True)

        # Selector found 4; structured cross-check adds the missed 5th.
        assert len(result.reviews) == 5
        # Agreement = structured found by chosen method / structured = 4/5.
        assert result.structured_agreement == pytest.approx(0.8)
        assert result.discarded.get("structured_added") == 1

    @mock_aws
    def test_unverifiable_structured_counted_in_discarded(self) -> None:
        _create_rate_limit_table()
        _install(_ScriptedClient([]))
        # Structured data claims 5 reviews but the page only renders 3 cards, so
        # 2 structured bodies are not on the page → unverifiable, dropped.
        html = _html_with_structured(card_count=3, structured_count=5)
        plan = _selectors_plan(per_page_rate=3)

        result = extract_page(html, "https://example.com/reviews", plan, is_last=True)

        assert result.discarded.get("structured_unverified") == 2


# ---------------------------------------------------------------------------
# PageResult completeness (Requirement 6.5)
# ---------------------------------------------------------------------------


class TestPageResultCompleteness:
    @mock_aws
    def test_all_fields_populated_on_representative_case(self) -> None:
        _create_rate_limit_table()
        _install(_ScriptedClient([]))
        html = _html_with_structured(card_count=5, structured_count=5)
        plan = _selectors_plan(per_page_rate=5)

        result = extract_page(html, "https://example.com/reviews?page=1", plan, is_last=False)

        assert isinstance(result, PageResult)
        assert result.method_used == "selectors"
        assert result.fallback is False
        assert len(result.reviews) == 5
        assert isinstance(result.discarded, dict)
        assert result.structured_agreement == 1.0
        # Next page: the plan's selector rule resolves a.next → page=2.
        assert result.next_page.url is not None
        assert result.next_page.rule_used == "plan_selector"
        assert result.blocker is None
        # No Locator ran, so reported_total comes from structured review_count.
        assert result.reported_total == 5


# ---------------------------------------------------------------------------
# extract_by_selectors unit coverage (text/dup/too-short)
# ---------------------------------------------------------------------------


class TestExtractBySelectors:
    def test_too_short_text_discarded(self) -> None:
        html = (
            "<html><body><article class='review-card'>"
            "<p class='body'>short</p></article></body></html>"
        )
        plan = _selectors_plan()
        reviews, discarded = extract_by_selectors(html, plan)
        assert reviews == []
        assert discarded.get("too_short") == 1

    def test_duplicate_text_discarded(self) -> None:
        card = "<article class='review-card'><p class='body'>" + _BODIES[0] + "</p></article>"
        html = f"<html><body>{card}{card}</body></html>"
        plan = _selectors_plan()
        reviews, discarded = extract_by_selectors(html, plan)
        assert len(reviews) == 1
        assert discarded.get("duplicate") == 1

    def test_missing_item_selector_yields_nothing(self) -> None:
        html = _reviews_html(5)
        plan = _selectors_plan()
        plan.selectors.item = None
        reviews, discarded = extract_by_selectors(html, plan)
        assert reviews == []
        assert discarded == {}

    def test_text_falls_back_to_item_text_when_no_text_selector(self) -> None:
        html = (
            "<html><body><article class='review-card'>"
            f"<div>{_BODIES[0]}</div></article></body></html>"
        )
        plan = _selectors_plan()
        plan.selectors.text = None  # No text selector → read the item's own text.
        reviews, _ = extract_by_selectors(html, plan)
        assert len(reviews) == 1
        assert _BODIES[0] in reviews[0].text


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_should_fall_back_boundary(self) -> None:
        plan = _selectors_plan(per_page_rate=5)
        # Exactly half (2.5) → yield of 2 is below, yield of 3 is not.
        assert extract._should_fall_back(2, plan, is_last=False) is True
        assert extract._should_fall_back(3, plan, is_last=False) is False
        # Last page never falls back.
        assert extract._should_fall_back(0, plan, is_last=True) is False
