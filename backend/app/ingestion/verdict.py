"""The viability :class:`Verdict` and the verdict-building seam.

A :class:`Verdict` is the Viability Check's result for one URL: a label
(``will_work`` / ``limited`` / ``wont_work``), plain-language reasons, warnings,
and an :class:`Evidence` object describing what was found. It mirrors the JSON
example in the dataset-ingestion design ("Viability assessment (AI-first)") and
is what the check handler writes to the Check Session, what the UI renders as a
verdict card, and what Add stores under ``status_detail.viability``.

This module owns the verdict *vocabulary* and ``build_verdict()``. It
implements the full verdict rules (Requirement 3.4–3.6): the per-label
thresholds (all taken from configuration, never literals), the plain-language
reasons and evidence (Requirement 3.7), the sample reviews read from the page,
and the robots.txt warning that never changes a label on its own (Requirement
3.12). The AI-unavailable fallback capping — the exact "AI page reading
unavailable" wording — is refined by task 3.4; the capping hook (degraded plans
→ ``limited``) already lives in :func:`_classify`.

The models are plain dataclasses so they are cheap to build in a worker and
serialize to the stored JSON shape via :meth:`Verdict.to_dict`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.extraction.models import ExtractionPlan, PageResult, VerifiedReview

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The three verdict labels (Requirement 3.4–3.6).
VerdictLabel = Literal["will_work", "limited", "wont_work"]

#: How many sample reviews a verdict carries so the analyst can see what was
#: found (Requirement 3.7: "two or three sample reviews"). Task 3.3 may refine
#: how samples are chosen; the count lives here as the shared cap.
SAMPLE_COUNT = 3


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """One sample review shown on a verdict card (text read from the page)."""

    text: str
    rating: float | None = None
    date: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"text": self.text, "rating": self.rating, "date": self.date}


@dataclass(frozen=True)
class Evidence:
    """The structured evidence behind a verdict (design JSON ``evidence``).

    Every field maps to a key in the design's example object so the stored
    verdict and the UI card read from one shape:
    ``reviews_verified``, ``reviews_rejected``, ``method``, ``pagination``,
    ``reported_total``, ``blocker``, ``locator_confidence``, ``page_title``,
    ``main_status``, and ``samples``.
    """

    reviews_verified: int = 0
    reviews_rejected: int = 0
    method: str | None = None
    pagination: bool = False
    reported_total: int | None = None
    blocker: str | None = None
    locator_confidence: str | None = None
    page_title: str = ""
    main_status: int | None = None
    samples: list[Sample] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "reviews_verified": self.reviews_verified,
            "reviews_rejected": self.reviews_rejected,
            "method": self.method,
            "pagination": self.pagination,
            "reported_total": self.reported_total,
            "blocker": self.blocker,
            "locator_confidence": self.locator_confidence,
            "page_title": self.page_title,
            "main_status": self.main_status,
            "samples": [s.to_dict() for s in self.samples],
        }


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """The Viability Check result for one URL.

    Attributes:
        verdict: The label (``will_work`` / ``limited`` / ``wont_work``).
        reasons: Plain-language reasons for the label (Requirement 3.7).
        warnings: Non-blocking warnings, e.g. a robots.txt disallow
            (Requirement 3.12). A warning never changes the label on its own.
        evidence: The structured :class:`Evidence` behind the verdict.
    """

    verdict: VerdictLabel
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: Evidence = field(default_factory=Evidence)

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "evidence": self.evidence.to_dict(),
        }


# ---------------------------------------------------------------------------
# Sample selection
# ---------------------------------------------------------------------------


def _samples(reviews: list[VerifiedReview], count: int = SAMPLE_COUNT) -> list[Sample]:
    """Pick up to *count* sample reviews from the first page's verified reviews.

    Reviews are read from the page by code (never AI-written), so the samples
    are literal page text (an engineering rule). The first few verified reviews
    in document order are shown, so the analyst sees representative page text.
    """
    return [Sample(text=r.text, rating=r.rating, date=r.date) for r in reviews[:count]]


# ---------------------------------------------------------------------------
# Plain-language helpers
# ---------------------------------------------------------------------------


def _plural(count: int, noun: str) -> str:
    """Return ``"1 review"`` / ``"2 reviews"`` — a count with a pluralised noun.

    Numbers are grouped with thousands separators so the reasons read naturally
    (for example ``"1,540 reviews"``).
    """
    suffix = "" if count == 1 else "s"
    return f"{count:,} {noun}{suffix}"


# ---------------------------------------------------------------------------
# Evidence assembly from a plan + first-page result
# ---------------------------------------------------------------------------


def evidence_from_plan(plan: ExtractionPlan, first_page: PageResult) -> Evidence:
    """Assemble the :class:`Evidence` from a plan and its first-page result.

    Shared by both the normal and blocked/degraded paths so the stored shape is
    consistent. ``pagination`` is true when the plan carries a usable next-page
    rule; ``blocker`` comes from the first-page result; counts come from the
    first-page statistics.
    """
    return Evidence(
        reviews_verified=plan.first_page.verified,
        reviews_rejected=plan.first_page.discarded,
        method=plan.method,
        pagination=plan.next_page_rule.type != "none",
        reported_total=plan.reported_total,
        blocker=first_page.blocker,
        locator_confidence=plan.confidence,
        page_title="",
        main_status=None,
        samples=_samples(first_page.reviews),
    )


# ---------------------------------------------------------------------------
# Verdict building (the seam task 3.3 / 3.4 refine)
# ---------------------------------------------------------------------------


#: Plain-language names for each extraction method, used in the reasons so the
#: analyst reads "read with page selectors" rather than the internal literal.
_METHOD_PHRASING: dict[str, str] = {
    "selectors": "read with page selectors",
    "ai_direct": "read by AI on each page",
    "structured": "read from the page's structured data",
}

#: Plain-language names for each blocker, used in the ``wont_work`` reason.
_BLOCKER_PHRASING: dict[str, str] = {
    "captcha": "The page is a bot or CAPTCHA challenge",
    "login_wall": "The page requires signing in to see reviews",
    "consent_wall": "The page is blocked by a cookie or consent wall",
    "empty": "The page loaded no readable content",
}


def build_verdict(
    plan: ExtractionPlan,
    first_page: PageResult,
    *,
    page_title: str,
    main_status: int | None,
    min_reviews: int,
    reported_total_multiplier: float = 2.0,
    max_rejection_fraction: float = 0.20,
    robots_warning: str | None = None,
) -> Verdict:
    """Build a :class:`Verdict` from a plan + first-page outcome.

    This owns the full verdict rules (Requirement 3.4–3.6), the plain-language
    reasons and evidence (Requirement 3.7), and the robots warning (Requirement
    3.12). All thresholds are passed in from configuration — none are literals.

    The label is decided by :func:`_classify`:

    - ``wont_work`` when a blocker is present or no reviews were verified
      (Requirement 3.5);
    - ``will_work`` when at least *min_reviews* were verified, no blocker, the
      Locator's confidence is not low, too many of the Locator's reviews did not
      fail verification, and either a next page was found or the page does not
      report many more reviews than it shows (Requirement 3.4);
    - ``limited`` in every other case (Requirement 3.6), including the degraded
      (AI-unavailable) plan which is capped at ``limited`` (Requirement 3.13;
      task 3.4 refines the exact reason wording).

    A robots.txt disallow is attached as a *warning* and never changes the label
    on its own (Requirement 3.12).
    """
    base = evidence_from_plan(plan, first_page)
    evidence = Evidence(
        reviews_verified=base.reviews_verified,
        reviews_rejected=base.reviews_rejected,
        method=base.method,
        pagination=base.pagination,
        reported_total=base.reported_total,
        blocker=base.blocker,
        locator_confidence=base.locator_confidence,
        page_title=page_title,
        main_status=main_status,
        samples=base.samples,
    )

    warnings: list[str] = []
    if robots_warning:
        warnings.append(_robots_phrasing(robots_warning))

    label, reasons = _classify(
        plan,
        evidence,
        min_reviews=min_reviews,
        reported_total_multiplier=reported_total_multiplier,
        max_rejection_fraction=max_rejection_fraction,
    )
    return Verdict(verdict=label, reasons=reasons, warnings=warnings, evidence=evidence)


def _robots_phrasing(robots_warning: str) -> str:
    """Return clear plain-language phrasing for a robots.txt disallow warning.

    The robots check (``app.ingestion.robots``) supplies a short machine-ish
    string; this makes it read as a clear, non-blocking warning on the card.
    An already-clear sentence (one that mentions "robots") is passed through so
    a more specific upstream message is preserved.
    """
    if "robots" in robots_warning.lower():
        return robots_warning
    return (
        "The site's robots.txt asks crawlers not to fetch this page; "
        "this is a warning only and does not change the verdict"
    )


def _reported_many_more(
    reported_total: int | None,
    verified: int,
    *,
    multiplier: float,
) -> bool:
    """True when the page reports many more reviews than it shows.

    "Many more" means the reported total is above ``verified × multiplier``
    (Requirement 3.4/3.6). An unknown or zero reported total is never "many
    more", and a reported total at or below what was verified never is either.
    """
    if reported_total is None:
        return False
    return reported_total > verified * multiplier


def _rejection_fraction(verified: int, rejected: int) -> float:
    """Return the fraction of the Locator's reviews that failed verification.

    ``rejected / (verified + rejected)``; zero when the Locator returned nothing
    (so a page with no reviews is handled by the blocker/zero rules, not here).
    """
    total = verified + rejected
    if total == 0:
        return 0.0
    return rejected / total


def _classify(
    plan: ExtractionPlan,
    evidence: Evidence,
    *,
    min_reviews: int,
    reported_total_multiplier: float,
    max_rejection_fraction: float,
) -> tuple[VerdictLabel, list[str]]:
    """Map a plan outcome to a verdict label and plain-language reasons.

    Implements the verdict rules in Requirement 3.4–3.6. Thresholds come from
    the caller (configuration), never literals here.
    """
    verified = evidence.reviews_verified
    rejected = evidence.reviews_rejected

    # wont_work: a blocker, or nothing verified (Requirement 3.5).
    if evidence.blocker is not None:
        reason = _BLOCKER_PHRASING.get(
            evidence.blocker, f"A blocker was found on the page ({evidence.blocker})"
        )
        return "wont_work", [reason]
    if verified == 0:
        return "wont_work", ["No reviews could be read from this page"]

    # Degraded (AI-unavailable) plans are capped at limited (Requirement 3.13).
    # Task 3.4 refines the exact reason wording.
    if plan.degraded:
        return "limited", ["AI page reading unavailable"]

    has_next = evidence.pagination
    reported = evidence.reported_total
    confident = plan.confidence != "low"
    enough = verified >= min_reviews
    many_more = _reported_many_more(reported, verified, multiplier=reported_total_multiplier)
    too_many_rejected = _rejection_fraction(verified, rejected) > max_rejection_fraction

    # The headline reason always says what was found and how it will be read.
    reasons: list[str] = [f"{_plural(verified, 'review')} found and verified on this page"]
    method_phrase = _METHOD_PHRASING.get(evidence.method or "")
    if method_phrase:
        reasons.append(method_phrase.capitalize())

    # will_work (Requirement 3.4): enough verified, confident, not too many
    # failed verification, and either a next page exists or the page is not
    # hiding many more than it showed.
    if enough and confident and not too_many_rejected and (has_next or not many_more):
        if has_next:
            reasons.append("Next page link found")
        if reported is not None:
            reasons.append(f"{_plural(reported, 'review')} reported in total")
        return "will_work", reasons

    # limited (Requirement 3.6): every other case. Add the specific reasons.
    if not enough:
        reasons.append(f"Fewer than {_plural(min_reviews, 'review')} could be read")
    if not confident:
        reasons.append("Low confidence reading this page")
    if too_many_rejected:
        reasons.append(
            f"{_plural(rejected, 'review')} the page reader found could not be "
            "confirmed on the page"
        )
    if many_more and not has_next:
        assert reported is not None  # _reported_many_more is False when None
        reasons.append(
            f"{_plural(reported, 'review')} reported but only "
            f"{verified:,} shown and no next page found"
        )
    return "limited", reasons


def blocked_verdict(
    plan: ExtractionPlan,
    first_page: PageResult,
    *,
    blocker: str,
    reason: str,
    page_title: str,
    main_status: int | None,
) -> Verdict:
    """Build a ``wont_work`` verdict for a certain pre-scan blocker (no AI).

    Used by the pre-scan short-circuit: the rule-based scan found a definite
    blocker, so the verdict is ``wont_work`` with the blocker recorded in the
    evidence and no AI spent (Requirement 3.2 / 3.5).
    """
    evidence = Evidence(
        reviews_verified=0,
        reviews_rejected=0,
        method=plan.method,
        pagination=False,
        reported_total=None,
        blocker=blocker,
        locator_confidence=None,
        page_title=page_title,
        main_status=main_status,
        samples=[],
    )
    return Verdict(verdict="wont_work", reasons=[reason], warnings=[], evidence=evidence)
