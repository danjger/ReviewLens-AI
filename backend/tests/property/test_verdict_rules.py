"""Property-based test for app.ingestion.verdict.build_verdict (11).

- **Property 4: Verdict rules are consistent.** *For any* plan outcome, a
  blocker or zero verified reviews SHALL give ``wont_work``, and ``will_work``
  SHALL only be given with at least the minimum verified reviews and no blocker.
  Validates: Requirements 3.4, 3.5, 3.6

This is the pure-logic property (no AI, no S3, no DB). It drives the real
``build_verdict`` over a wide space of plan/first-page outcomes — a verified
count, a discarded count, a blocker that is present or absent, a confidence
level, a next-page rule, a reported total, and a degraded flag — using
thresholds straight from configuration (never literals), exactly as
``viability.assess`` calls it.

The three invariants asserted hold for every input:

1. A blocker present ⇒ ``wont_work``.
2. Zero verified reviews ⇒ ``wont_work``.
3. ``will_work`` ⇒ at least ``min_reviews`` verified **and** no blocker.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import get_settings
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
from hypothesis import given
from hypothesis import strategies as st

# Extraction-engine blocker labels, plus ``None`` for "no blocker".
_BLOCKERS = st.sampled_from([None, "captcha", "login_wall", "consent_wall", "empty"])
_CONFIDENCE = st.sampled_from([None, "low", "medium", "high"])
_METHOD = st.sampled_from(["selectors", "ai_direct", "structured"])


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@st.composite
def _plan_and_page(draw: st.DrawFn) -> tuple[ExtractionPlan, PageResult]:
    """Generate a coherent (plan, first-page result) pair for the verdict.

    ``build_verdict`` reads the verified/discarded counts and the next-page rule
    from the *plan* and the blocker from the *first-page result*, so both are
    generated together and kept consistent (the plan's ``first_page`` stats and
    the page's ``blocker`` are what the verdict actually consumes).
    """
    verified = draw(st.integers(min_value=0, max_value=50))
    discarded = draw(st.integers(min_value=0, max_value=50))
    blocker = draw(_BLOCKERS)
    confidence = draw(_CONFIDENCE)
    method = draw(_METHOD)
    degraded = draw(st.booleans())
    reported_total = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=5000)))
    has_next = draw(st.booleans())

    next_rule = (
        NextPageRule(type="selector", css="a.next") if has_next else NextPageRule(type="none")
    )

    # A handful of verified-review stand-ins so samples (page text) are present
    # when the count is positive; text is literal, never AI-generated.
    reviews = [
        VerifiedReview(text=f"review {i}", source_ref=f"e{i}") for i in range(min(verified, 3))
    ]

    plan = ExtractionPlan(
        version=1,
        created_at=_now_iso(),
        locator_model="test-model",
        prompt_version="v1",
        method=method,
        selectors=LocatorSelectors(),
        rating_scale=None,
        next_page_rule=next_rule,
        first_page=FirstPageStats(verified=verified, discarded=discarded),
        reported_total=reported_total,
        entity_hint=None,
        confidence=confidence,
        degraded=degraded,
    )
    page = PageResult(
        reviews=reviews,
        method_used=method,
        fallback=False,
        discarded={},
        structured_agreement=None,
        next_page=NextPage(url=None, rule_used="none"),
        blocker=blocker,
        reported_total=reported_total,
    )
    return plan, page


@given(pair=_plan_and_page(), robots_warning=st.one_of(st.none(), st.just("blocked by robots")))
def test_verdict_rules_are_consistent(
    pair: tuple[ExtractionPlan, PageResult], robots_warning: str | None
) -> None:
    """Property 4: Verdict rules are consistent.

    A blocker or zero verified reviews SHALL give wont_work; will_work SHALL
    only be given with at least the minimum verified reviews and no blocker.
    Validates: Requirements 3.4, 3.5, 3.6
    """
    plan, page = pair
    settings = get_settings()

    verdict = build_verdict(
        plan,
        page,
        page_title="Some Reviews",
        main_status=200,
        min_reviews=settings.viability_min_reviews,
        reported_total_multiplier=settings.viability_reported_total_multiplier,
        max_rejection_fraction=settings.viability_max_rejection_fraction,
        robots_warning=robots_warning,
    )

    verified = plan.first_page.verified
    blocker = page.blocker

    # (1) A blocker ⇒ wont_work (Requirement 3.5).
    if blocker is not None:
        assert verdict.verdict == "wont_work"

    # (2) Zero verified ⇒ wont_work (Requirement 3.5).
    if verified == 0:
        assert verdict.verdict == "wont_work"

    # (3) will_work ⇒ enough verified and no blocker (Requirement 3.4).
    if verdict.verdict == "will_work":
        assert blocker is None
        assert verified >= settings.viability_min_reviews

    # A robots warning never changes the label on its own: it only ever appears
    # as a warning, never as the reason the label was decided (Requirement 3.12).
    if robots_warning is not None:
        assert verdict.warnings  # the warning is surfaced
