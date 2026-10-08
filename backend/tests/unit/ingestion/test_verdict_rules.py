"""Table-driven unit tests for the verdict rules (dataset-ingestion task 3.5).

These tests exercise :func:`app.ingestion.verdict.build_verdict` (and, through
it, ``_classify`` / the threshold helpers) directly, with hand-built
``ExtractionPlan`` + ``PageResult`` fixtures and no AI or I/O. They cover the
full decision matrix of Requirements 3.4–3.6, plus the robots.txt warning
(3.12) and a light touch of the AI-unavailable fallback (3.13 — the detailed
fallback ordering/wording lives in ``test_verdict_fallback.py``).

The decision matrix (all thresholds at their configuration defaults —
``viability_min_reviews`` = 5, ``viability_reported_total_multiplier`` = 2.0,
``viability_max_rejection_fraction`` = 0.20):

- **will_work** (Req 3.4): verified ≥ min, no blocker, confidence not low,
  rejection fraction ≤ threshold, AND (next page found OR reported_total ≤
  2× verified).
- **wont_work** (Req 3.5): any blocker, or zero verified reviews.
- **limited** (Req 3.6): every other case — too few verified, low confidence,
  rejection fraction > threshold, or many-more-reported with no next page; and
  a degraded (AI-unavailable) plan with reviews.

Thresholds are driven exactly as production drives them: the config defaults
are read via ``get_settings`` (reset per test) and passed into ``build_verdict``
as the ``min_reviews`` / ``reported_total_multiplier`` / ``max_rejection_fraction``
parameters, so the table pins the real production thresholds rather than magic
numbers invented by the test.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from app.core.config import Settings, get_settings
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


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Reset memoised settings so the default thresholds are in force."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fixture builders — a plan + first-page result constructed entirely by hand.
# ---------------------------------------------------------------------------


def _plan(
    *,
    verified: int,
    discarded: int = 0,
    method: str = "selectors",
    has_next: bool = True,
    reported_total: int | None = None,
    confidence: str | None = "high",
    degraded: bool = False,
) -> ExtractionPlan:
    """Build an ExtractionPlan with the knobs the verdict rules read.

    ``has_next`` controls the next-page rule (``selector`` vs ``none``), which
    is what ``evidence.pagination`` reflects. ``confidence`` of ``"low"`` is the
    only value that fails the "confidence not low" clause.
    """
    next_rule = (
        NextPageRule(type="selector", css=".next") if has_next else NextPageRule(type="none")
    )
    return ExtractionPlan(
        version=1,
        created_at="2024-01-02T03:04:05+00:00",
        locator_model="model-x",
        prompt_version="locator_v1",
        method=method,  # type: ignore[arg-type]
        selectors=LocatorSelectors(item=".r") if method == "selectors" else LocatorSelectors(),
        rating_scale=5.0,
        next_page_rule=next_rule,
        first_page=FirstPageStats(
            verified=verified,
            discarded=discarded,
            structured_count=0,
            per_page_rate=verified,
        ),
        reported_total=reported_total,
        entity_hint="Acme CRM",
        confidence=confidence,  # type: ignore[arg-type]
        degraded=degraded,
    )


def _page(
    *,
    verified: int,
    method: str = "selectors",
    has_next: bool = True,
    reported_total: int | None = None,
    blocker: str | None = None,
) -> PageResult:
    """Build the first-page result that accompanies a plan.

    The verified reviews carry literal page text (reviews are read from the
    page by code, never AI-written), so the samples are real page text.
    """
    reviews = [
        VerifiedReview(
            text=f"Review {i}: the product worked well and support was responsive",
            rating=5.0,
            date=None,
            source_ref=f"e{i}",
        )
        for i in range(verified)
    ]
    next_url = "https://example.com/reviews?page=2" if has_next else None
    return PageResult(
        reviews=reviews,
        method_used=method,  # type: ignore[arg-type]
        fallback=False,
        discarded={},
        structured_agreement=None,
        next_page=NextPage(
            url=next_url,
            rule_used="plan_rule" if has_next else "none",
            reason_if_none=None if has_next else "no_next",
        ),
        blocker=blocker,  # type: ignore[arg-type]
        reported_total=reported_total,
    )


def _build(
    plan: ExtractionPlan,
    page: PageResult,
    *,
    robots_warning: str | None = None,
):
    """Call ``build_verdict`` with the *production* thresholds from config.

    Thresholds come from the Settings defaults (reset per test), matching how
    ``viability.assess`` wires them, so the table pins real behaviour.
    """
    settings: Settings = get_settings()
    return build_verdict(
        plan,
        page,
        page_title="Acme CRM Reviews",
        main_status=200,
        min_reviews=settings.viability_min_reviews,
        reported_total_multiplier=settings.viability_reported_total_multiplier,
        max_rejection_fraction=settings.viability_max_rejection_fraction,
        robots_warning=robots_warning,
    )


# ---------------------------------------------------------------------------
# The decision table.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One row of the verdict decision table."""

    name: str
    plan: ExtractionPlan
    page: PageResult
    expected: str
    # A substring expected somewhere in the reasons (robust contains-check).
    reason_contains: str | None = None


def _case(
    name: str,
    *,
    verified: int,
    expected: str,
    discarded: int = 0,
    method: str = "selectors",
    has_next: bool = True,
    reported_total: int | None = None,
    confidence: str | None = "high",
    degraded: bool = False,
    blocker: str | None = None,
    reason_contains: str | None = None,
) -> Case:
    """Build a Case with a plan + page that share the same shape knobs."""
    return Case(
        name=name,
        plan=_plan(
            verified=verified,
            discarded=discarded,
            method=method,
            has_next=has_next,
            reported_total=reported_total,
            confidence=confidence,
            degraded=degraded,
        ),
        page=_page(
            verified=verified,
            method=method,
            has_next=has_next,
            reported_total=reported_total,
            blocker=blocker,
        ),
        expected=expected,
        reason_contains=reason_contains,
    )


# min_reviews default = 5; multiplier = 2.0; max_rejection_fraction = 0.20.
_CASES: list[Case] = [
    # --- will_work (Req 3.4) ---------------------------------------------
    _case(
        "will_work_typical_with_next_page",
        verified=24,
        has_next=True,
        reported_total=1540,
        expected="will_work",
        reason_contains="Next page",
    ),
    _case(
        "will_work_exactly_min_reviews",
        verified=5,  # exactly the minimum
        has_next=True,
        reported_total=5,
        expected="will_work",
    ),
    _case(
        "will_work_no_next_but_reported_not_more_than_shown",
        verified=30,
        has_next=False,
        reported_total=None,
        expected="will_work",
    ),
    _case(
        "will_work_no_next_reported_exactly_twice_verified",
        # reported_total exactly 2x verified with no next page: "not more than
        # twice" → still will_work (boundary, strictly-greater is the cutoff).
        verified=10,
        has_next=False,
        reported_total=20,
        expected="will_work",
        reason_contains="reported in total",
    ),
    _case(
        "will_work_reported_above_twice_but_next_page_found",
        # reported_total just above 2x verified, but a next page exists → the
        # "many more" clause is excused by the next page (Req 3.4).
        verified=10,
        has_next=True,
        reported_total=21,
        expected="will_work",
        reason_contains="Next page",
    ),
    _case(
        "will_work_rejection_fraction_exactly_at_threshold",
        # 2 rejected of (8 + 2) = 0.20, exactly at threshold → NOT limited by
        # the rejection rule alone (strictly-greater is the cutoff).
        verified=8,
        discarded=2,
        has_next=True,
        expected="will_work",
    ),
    _case(
        "will_work_ai_direct_method_phrasing",
        verified=12,
        method="ai_direct",
        has_next=True,
        expected="will_work",
        # The method phrase is capitalised by production (``.capitalize()``),
        # which lower-cases the rest, so match a stable fragment.
        reason_contains="read by ai on each page",
    ),
    # --- wont_work (Req 3.5) ---------------------------------------------
    _case(
        "wont_work_captcha_blocker",
        verified=50,  # plenty of reviews, but a blocker overrides everything
        has_next=True,
        blocker="captcha",
        expected="wont_work",
        reason_contains="CAPTCHA",
    ),
    _case(
        "wont_work_login_wall_blocker",
        verified=40,
        blocker="login_wall",
        expected="wont_work",
        reason_contains="signing in",
    ),
    _case(
        "wont_work_consent_wall_blocker",
        verified=12,
        blocker="consent_wall",
        expected="wont_work",
        reason_contains="consent wall",
    ),
    _case(
        "wont_work_empty_blocker",
        verified=0,
        blocker="empty",
        expected="wont_work",
        reason_contains="no readable content",
    ),
    _case(
        "wont_work_zero_verified_no_blocker",
        verified=0,
        has_next=False,
        expected="wont_work",
        reason_contains="No reviews could be read",
    ),
    # --- limited (Req 3.6) -----------------------------------------------
    _case(
        "limited_fewer_than_min_verified",
        verified=3,  # > 0 but < 5
        has_next=False,
        expected="limited",
        reason_contains="Fewer than",
    ),
    _case(
        "limited_low_locator_confidence",
        verified=24,
        has_next=True,
        confidence="low",
        expected="limited",
        reason_contains="Low confidence",
    ),
    _case(
        "limited_rejection_fraction_above_threshold",
        # verified=10, rejected=5 → 5/15 = 0.33 > 0.20 → limited.
        verified=10,
        discarded=5,
        has_next=True,
        expected="limited",
        reason_contains="could not be",
    ),
    _case(
        "limited_rejection_fraction_just_above_threshold",
        # verified=8, rejected=3 → 3/11 ≈ 0.273 > 0.20 → limited.
        verified=8,
        discarded=3,
        has_next=True,
        expected="limited",
        reason_contains="could not be",
    ),
    _case(
        "limited_many_more_reported_no_next_page",
        # reported_total just above 2x verified AND no next page → limited.
        verified=10,
        has_next=False,
        reported_total=21,
        expected="limited",
        reason_contains="no next page found",
    ),
    _case(
        "limited_degraded_plan_with_reviews",
        # AI-unavailable fallback: degraded plan with reviews caps at limited.
        verified=12,
        method="structured",
        has_next=False,
        degraded=True,
        confidence=None,
        expected="limited",
        reason_contains=_AI_UNAVAILABLE_REASON,
    ),
    # --- fallback (Req 3.13), light touch — see test_verdict_fallback.py --
    _case(
        "degraded_plan_zero_reviews_is_wont_work",
        verified=0,
        method="structured",
        has_next=False,
        degraded=True,
        confidence=None,
        expected="wont_work",
        reason_contains="No reviews could be read",
    ),
]


@pytest.mark.parametrize("case", _CASES, ids=[c.name for c in _CASES])
def test_verdict_decision_table(case: Case) -> None:
    """Each row maps a plan outcome to the expected label (Req 3.4–3.6, 3.13)."""
    verdict = _build(case.plan, case.page)

    assert verdict.verdict == case.expected, (
        f"{case.name}: expected {case.expected}, got {verdict.verdict} (reasons={verdict.reasons})"
    )

    # Reasons are always non-empty plain language.
    assert verdict.reasons, f"{case.name}: reasons must be non-empty"
    assert all(isinstance(r, str) and r.strip() for r in verdict.reasons)

    if case.reason_contains is not None:
        joined = " | ".join(verdict.reasons)
        # Case-insensitive contains: phrasing capitalisation can vary (reasons
        # are capitalised for display) but the signal word must be present.
        assert case.reason_contains.lower() in joined.lower(), (
            f"{case.name}: expected a reason containing {case.reason_contains!r}, "
            f"got {verdict.reasons}"
        )


@pytest.mark.parametrize("case", _CASES, ids=[c.name for c in _CASES])
def test_evidence_object_populated(case: Case) -> None:
    """The evidence object carries method, pagination, counts, blocker, totals."""
    verdict = _build(case.plan, case.page)
    evidence = verdict.evidence

    # Counts mirror the first-page statistics.
    assert evidence.reviews_verified == case.plan.first_page.verified
    assert evidence.reviews_rejected == case.plan.first_page.discarded
    # Method and reported total come straight from the plan.
    assert evidence.method == case.plan.method
    assert evidence.reported_total == case.plan.reported_total
    # Pagination reflects the next-page rule.
    assert evidence.pagination is (case.plan.next_page_rule.type != "none")
    # Blocker comes from the first-page result.
    assert evidence.blocker == case.page.blocker
    # Capture metadata is threaded through.
    assert evidence.page_title == "Acme CRM Reviews"
    assert evidence.main_status == 200
    # Samples are literal page text when reviews exist, capped at three.
    assert len(evidence.samples) == min(3, len(case.page.reviews))
    for sample, review in zip(evidence.samples, case.page.reviews, strict=False):
        assert sample.text == review.text


# ---------------------------------------------------------------------------
# Boundary focus tests (called out explicitly so a regression names itself).
# ---------------------------------------------------------------------------


class TestReportedTotalBoundary:
    """The ``reported_total ≤ 2× verified`` boundary (Req 3.4 vs 3.6)."""

    def test_exactly_twice_with_no_next_page_is_will_work(self) -> None:
        # "no more than twice the verified count" → equality is allowed.
        verdict = _build(
            _plan(verified=10, has_next=False, reported_total=20),
            _page(verified=10, has_next=False, reported_total=20),
        )
        assert verdict.verdict == "will_work"

    def test_just_above_twice_with_no_next_page_is_limited(self) -> None:
        verdict = _build(
            _plan(verified=10, has_next=False, reported_total=21),
            _page(verified=10, has_next=False, reported_total=21),
        )
        assert verdict.verdict == "limited"
        assert "no next page found" in " | ".join(verdict.reasons)

    def test_just_above_twice_with_next_page_is_will_work(self) -> None:
        verdict = _build(
            _plan(verified=10, has_next=True, reported_total=21),
            _page(verified=10, has_next=True, reported_total=21),
        )
        assert verdict.verdict == "will_work"


class TestRejectionFractionBoundary:
    """The ``rejection fraction ≤ 0.20`` boundary (Req 3.6)."""

    def test_exactly_at_threshold_is_not_limited_by_rejections(self) -> None:
        # 2 / (8 + 2) = 0.20 exactly → not held back by the rejection rule.
        verdict = _build(
            _plan(verified=8, discarded=2, has_next=True),
            _page(verified=8, has_next=True),
        )
        assert verdict.verdict == "will_work"

    def test_just_above_threshold_is_limited(self) -> None:
        # 3 / (8 + 3) ≈ 0.273 > 0.20 → limited.
        verdict = _build(
            _plan(verified=8, discarded=3, has_next=True),
            _page(verified=8, has_next=True),
        )
        assert verdict.verdict == "limited"


class TestMinReviewsBoundary:
    """The ``verified ≥ min_reviews`` boundary (Req 3.4 vs 3.6)."""

    def test_exactly_min_is_will_work(self) -> None:
        verdict = _build(
            _plan(verified=5, has_next=True),
            _page(verified=5, has_next=True),
        )
        assert verdict.verdict == "will_work"

    def test_one_below_min_is_limited(self) -> None:
        verdict = _build(
            _plan(verified=4, has_next=True),
            _page(verified=4, has_next=True),
        )
        assert verdict.verdict == "limited"
        assert "Fewer than" in " | ".join(verdict.reasons)


# ---------------------------------------------------------------------------
# Fallback reason wording (exact) — complements test_verdict_fallback.py.
# ---------------------------------------------------------------------------


def test_degraded_plan_uses_exact_fallback_reason() -> None:
    """A degraded plan with reviews caps at limited with the exact wording."""
    verdict = _build(
        _plan(verified=12, method="structured", has_next=False, degraded=True, confidence=None),
        _page(verified=12, method="structured", has_next=False),
    )
    assert verdict.verdict == "limited"
    # The fallback reason must be exact (Req 3.13), not a paraphrase.
    assert verdict.reasons == [_AI_UNAVAILABLE_REASON]


# ---------------------------------------------------------------------------
# robots.txt warning: adds a warning, never changes the label (Req 3.12).
# ---------------------------------------------------------------------------


class TestRobotsWarning:
    """A robots disallow adds a warning without changing the verdict label."""

    def test_robots_warning_preserved_on_will_work(self) -> None:
        plan = _plan(verified=24, has_next=True, reported_total=30)
        page = _page(verified=24, has_next=True, reported_total=30)

        without = _build(plan, page)
        with_robots = _build(plan, page, robots_warning="disallowed by robots.txt")

        # Same label; warning added.
        assert without.verdict == with_robots.verdict == "will_work"
        assert without.warnings == []
        assert with_robots.warnings == ["disallowed by robots.txt"]
        # The warning doesn't leak into the reasons.
        assert "robots" not in " | ".join(with_robots.reasons).lower()

    def test_robots_warning_does_not_rescue_wont_work(self) -> None:
        plan = _plan(verified=0, has_next=False)
        page = _page(verified=0, has_next=False)

        verdict = _build(plan, page, robots_warning="disallowed by robots.txt")

        assert verdict.verdict == "wont_work"
        assert verdict.warnings == ["disallowed by robots.txt"]

    def test_generic_robots_phrasing_when_message_lacks_keyword(self) -> None:
        # A machine-ish warning without the word "robots" is expanded into a
        # clear sentence (and still mentions robots.txt).
        plan = _plan(verified=24, has_next=True)
        page = _page(verified=24, has_next=True)

        verdict = _build(plan, page, robots_warning="disallow:/reviews")

        assert len(verdict.warnings) == 1
        assert "robots.txt" in verdict.warnings[0]
        assert verdict.verdict == "will_work"
