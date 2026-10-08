"""Unit tests for the AI-unavailable fallback in the verdict rules (task 3.4).

These tests pin the fallback behaviour required by Requirement 3.13:

    "IF the AI provider is unavailable or the global AI limit is reached, THEN
    the Check SHALL fall back to structured data only, SHALL mark the verdict at
    most ``limited`` with the reason 'AI page reading unavailable,' and SHALL let
    the analyst retry later."

They target :func:`app.ingestion.verdict.build_verdict` directly (not the
``assess`` orchestration, which ``test_viability.py`` covers, nor the full
verdict-rule table, which task 3.5 owns). The focus is narrow and complementary:

- the *ordering* that makes "at most ``limited``" correct — a degraded plan with
  structured reviews is ``limited``, but a degraded plan with **zero** verified
  reviews (or a blocker) is still ``wont_work`` and is never upgraded to
  ``limited`` (Requirement 3.5 vs 3.13);
- the *exact* reason wording "AI page reading unavailable";
- the evidence ``method`` reflecting structured-only (``"structured"``);
- the degraded flag surviving on the plan so a later retry is not blocked
  ("let the analyst retry later").
"""

from __future__ import annotations

import pytest
from app.extraction.models import (
    ExtractionPlan,
    FirstPageStats,
    LocatorSelectors,
    NextPage,
    NextPageRule,
    PageResult,
    VerifiedReview,
)
from app.ingestion.verdict import build_verdict

_AI_UNAVAILABLE_REASON = "AI page reading unavailable"


def _degraded_plan(*, verified: int, reported_total: int | None = None) -> ExtractionPlan:
    """Build a degraded, structured-only plan (as the AI-unavailable path emits).

    Mirrors ``app.extraction.plan._degraded_plan``: ``method="structured"``,
    no selectors, unknown rating scale, a ``type="none"`` next-page rule, no
    confidence, and ``degraded=True``.
    """
    return ExtractionPlan(
        version=1,
        created_at="2024-01-02T03:04:05+00:00",
        locator_model="model-x",
        prompt_version="locator_v1",
        method="structured",
        selectors=LocatorSelectors(),
        rating_scale=None,
        next_page_rule=NextPageRule(type="none"),
        first_page=FirstPageStats(
            verified=verified,
            discarded=0,
            structured_count=verified,
            per_page_rate=verified,
        ),
        reported_total=reported_total,
        entity_hint=None,
        confidence=None,
        degraded=True,
    )


def _degraded_page(*, verified: int, blocker: str | None = None) -> PageResult:
    """Build the first-page result that accompanies a degraded plan."""
    reviews = [
        VerifiedReview(
            text=f"Structured review {i}: shipped quickly and worked as described",
            rating=None,
            date=None,
            source_ref=f"jsonld:{i}",
        )
        for i in range(verified)
    ]
    return PageResult(
        reviews=reviews,
        method_used="structured",
        fallback=False,
        discarded={},
        structured_agreement=None,
        next_page=NextPage(url=None, rule_used="none", reason_if_none="ai_unavailable"),
        blocker=blocker,  # type: ignore[arg-type]
        reported_total=None,
    )


def _build(plan: ExtractionPlan, page: PageResult, *, min_reviews: int = 5):
    """Call ``build_verdict`` with fixed, representative thresholds."""
    return build_verdict(
        plan,
        page,
        page_title="Acme CRM Reviews",
        main_status=200,
        min_reviews=min_reviews,
    )


class TestDegradedWithStructuredReviews:
    """A degraded plan that still found structured reviews → ``limited``."""

    def test_capped_at_limited_with_exact_reason(self) -> None:
        plan = _degraded_plan(verified=12)
        page = _degraded_page(verified=12)

        verdict = _build(plan, page)

        assert verdict.verdict == "limited"
        assert verdict.reasons == [_AI_UNAVAILABLE_REASON]

    def test_many_structured_reviews_still_limited_never_will_work(self) -> None:
        # Even with plenty of reviews, a degraded plan must never be will_work.
        plan = _degraded_plan(verified=500, reported_total=500)
        page = _degraded_page(verified=500)

        verdict = _build(plan, page, min_reviews=5)

        assert verdict.verdict == "limited"
        assert verdict.reasons == [_AI_UNAVAILABLE_REASON]

    def test_evidence_method_is_structured_only(self) -> None:
        plan = _degraded_plan(verified=8)
        page = _degraded_page(verified=8)

        verdict = _build(plan, page)

        assert verdict.evidence.method == "structured"
        assert verdict.evidence.pagination is False
        assert verdict.evidence.reviews_verified == 8

    def test_degraded_flag_survives_for_later_retry(self) -> None:
        # "Let the analyst retry later": nothing about the verdict path mutates
        # or clears the degraded flag the retry logic relies on.
        plan = _degraded_plan(verified=3)
        page = _degraded_page(verified=3)

        _build(plan, page, min_reviews=5)

        assert plan.degraded is True


class TestDegradedWithZeroReviews:
    """A degraded plan with no reviews is still ``wont_work`` (ordering)."""

    def test_zero_verified_is_wont_work_not_limited(self) -> None:
        # Requirement 3.5: no reviews = wont_work. The degraded cap must not
        # upgrade a zero-review page to limited.
        plan = _degraded_plan(verified=0)
        page = _degraded_page(verified=0)

        verdict = _build(plan, page)

        assert verdict.verdict == "wont_work"
        assert verdict.reasons != [_AI_UNAVAILABLE_REASON]

    def test_blocker_on_degraded_plan_is_wont_work(self) -> None:
        # A blocker takes precedence over the degraded cap as well.
        plan = _degraded_plan(verified=0)
        page = _degraded_page(verified=0, blocker="captcha")

        verdict = _build(plan, page)

        assert verdict.verdict == "wont_work"
        assert verdict.evidence.blocker == "captcha"


@pytest.mark.parametrize("verified", [1, 2, 50])
def test_any_nonzero_degraded_is_limited(verified: int) -> None:
    """Any non-zero verified count on a degraded plan caps at ``limited``."""
    plan = _degraded_plan(verified=verified)
    page = _degraded_page(verified=verified)

    verdict = build_verdict(plan, page, page_title="t", main_status=200, min_reviews=5)

    assert verdict.verdict == "limited"
    assert verdict.reasons == [_AI_UNAVAILABLE_REASON]
