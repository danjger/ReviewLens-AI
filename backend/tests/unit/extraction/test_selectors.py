"""Unit tests for selector validation (Task 5.1).

Covers :func:`app.extraction.selectors.validate_selectors`, which applies a
Locator's suggested ``item`` selector to the original HTML and decides whether
the selectors are trustworthy by measuring agreement with the Verified Reviews
and over-selection of extra elements (Requirement 4.1).

Thresholds under test: a selector is valid only when agreement reaches
``min_agreement`` (default 0.8) **and** over-selection stays at or below
``SELECTOR_MAX_OVER_SELECTION`` (0.2).  Matching uses normalized containment
(the ``item`` wrapper's text contains the verified field text).

The property test for Property 8 ("Selector validation is sound") over
generated review lists is Task 5.3 and lives in ``tests/property/``; these are
focused example-based tests of the thresholds and edge cases.

_Validates: Requirements 4.1_
"""

from __future__ import annotations

from app.extraction.models import LocatorSelectors, VerifiedReview
from app.extraction.selectors import (
    SELECTOR_MAX_OVER_SELECTION,
    validate_selectors,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

#: A page of five review cards.  Each card wraps a body paragraph (the field
#: the verified text was read from) plus an author line, so the ``item``
#: selector's text contains — but does not equal — the verified text.
_FIVE_CARDS = (
    "<html><body><main>"
    + "".join(
        f'<article class="review-card">'
        f'<p class="body">{body}</p>'
        f'<span class="author">Reviewer {i}</span>'
        f"</article>"
        for i, body in enumerate(
            [
                "Great tool, the setup took an afternoon but it worked well overall.",
                "Support was responsive and fixed my issue within a single day.",
                "The dashboard is intuitive and the reports export cleanly to CSV.",
                "Pricing is fair for a small team and the onboarding was painless.",
                "Occasional slowness at peak hours but reliable the rest of the time.",
            ]
        )
    )
    + "</main></body></html>"
)

_FIVE_BODIES = [
    "Great tool, the setup took an afternoon but it worked well overall.",
    "Support was responsive and fixed my issue within a single day.",
    "The dashboard is intuitive and the reports export cleanly to CSV.",
    "Pricing is fair for a small team and the onboarding was painless.",
    "Occasional slowness at peak hours but reliable the rest of the time.",
]


def _verified(*texts: str) -> list[VerifiedReview]:
    """Build Verified Reviews from their body texts."""
    return [VerifiedReview(text=text) for text in texts]


def _all_verified() -> list[VerifiedReview]:
    """Verified Reviews matching every card body on :data:`_FIVE_CARDS`."""
    return _verified(*_FIVE_BODIES)


# ---------------------------------------------------------------------------
# Agreement and over-selection
# ---------------------------------------------------------------------------


class TestExactMatch:
    """A selector that reproduces every verified review and nothing extra."""

    def test_all_match_is_valid(self) -> None:
        result = validate_selectors(
            _FIVE_CARDS,
            LocatorSelectors(item="article.review-card"),
            _all_verified(),
            min_agreement=0.8,
        )

        assert result.is_valid is True
        assert result.agreement == 1.0
        assert result.over_selection == 0.0
        assert result.selected_count == 5
        assert result.matched_verified == 5


class TestAgreementThreshold:
    """Agreement must reach ``min_agreement`` for validity."""

    def test_at_threshold_is_valid(self) -> None:
        """4 of 5 verified reviews matched = 0.8 agreement, exactly the bar."""
        # Verified includes four real bodies plus one body that is not on the
        # page, so one of five fails to match: agreement 0.8.
        verified = _verified(
            *_FIVE_BODIES[:4],
            "This review text never appears anywhere on the rendered page body.",
        )
        result = validate_selectors(
            _FIVE_CARDS,
            LocatorSelectors(item="article.review-card"),
            verified,
            min_agreement=0.8,
        )

        assert result.agreement == 0.8
        assert result.is_valid is True

    def test_below_threshold_is_invalid(self) -> None:
        """3 of 5 verified reviews matched = 0.6 agreement, below the bar."""
        verified = _verified(
            *_FIVE_BODIES[:3],
            "A review body that is absent from the page entirely, number one.",
            "A review body that is absent from the page entirely, number two.",
        )
        result = validate_selectors(
            _FIVE_CARDS,
            LocatorSelectors(item="article.review-card"),
            verified,
            min_agreement=0.8,
        )

        assert result.agreement == 0.6
        assert result.is_valid is False


class TestOverSelection:
    """Even with high agreement, too many extra items invalidates selectors."""

    def test_over_selection_above_limit_is_invalid(self) -> None:
        # Add six non-review cards sharing a broad selector that also matches
        # the five review cards.  A selector that grabs all 11 would reproduce
        # all five verified texts (agreement 1.0) but 6/11 ≈ 0.55 of selected
        # items are extra, far above the 0.2 limit.
        noise = "".join(
            f'<div class="card">Unrelated promo block number {i} with filler text.</div>'
            for i in range(6)
        )
        html = (
            "<html><body><main>"
            + "".join(
                f'<div class="card"><p class="body">{body}</p></div>' for body in _FIVE_BODIES
            )
            + noise
            + "</main></body></html>"
        )
        result = validate_selectors(
            html,
            LocatorSelectors(item="div.card"),
            _all_verified(),
            min_agreement=0.8,
        )

        assert result.agreement == 1.0
        assert result.over_selection > SELECTOR_MAX_OVER_SELECTION
        assert result.is_valid is False

    def test_over_selection_at_limit_is_valid(self) -> None:
        """One extra item out of five selected = 0.2 over-selection, at the bar."""
        # Four review cards plus one noise card, all matched by ``div.card``.
        # The four verified reviews all match (agreement 1.0); the noise card is
        # the single extra: 1/5 = 0.2 over-selection, exactly allowed.
        html = (
            "<html><body><main>"
            + "".join(
                f'<div class="card"><p class="body">{body}</p></div>' for body in _FIVE_BODIES[:4]
            )
            + '<div class="card">A noise block with no verified review text here.</div>'
            + "</main></body></html>"
        )
        result = validate_selectors(
            html,
            LocatorSelectors(item="div.card"),
            _verified(*_FIVE_BODIES[:4]),
            min_agreement=0.8,
        )

        assert result.agreement == 1.0
        assert result.over_selection == SELECTOR_MAX_OVER_SELECTION
        assert result.is_valid is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Degenerate inputs always produce an invalid result, never a crash."""

    def test_no_item_selector_is_invalid(self) -> None:
        result = validate_selectors(
            _FIVE_CARDS,
            LocatorSelectors(item=None),
            _all_verified(),
            min_agreement=0.8,
        )

        assert result.is_valid is False
        assert result.agreement == 0.0

    def test_blank_item_selector_is_invalid(self) -> None:
        result = validate_selectors(
            _FIVE_CARDS,
            LocatorSelectors(item="   "),
            _all_verified(),
            min_agreement=0.8,
        )

        assert result.is_valid is False

    def test_selector_matching_nothing_is_invalid(self) -> None:
        result = validate_selectors(
            _FIVE_CARDS,
            LocatorSelectors(item="article.does-not-exist"),
            _all_verified(),
            min_agreement=0.8,
        )

        assert result.is_valid is False
        assert result.agreement == 0.0
        assert result.selected_count == 0

    def test_malformed_selector_is_invalid_without_crash(self) -> None:
        """A selector that throws is treated as zero yield (design: Error Handling)."""
        result = validate_selectors(
            _FIVE_CARDS,
            LocatorSelectors(item="article.review-card:::broken[["),
            _all_verified(),
            min_agreement=0.8,
        )

        assert result.is_valid is False

    def test_empty_verified_list_is_invalid(self) -> None:
        """With nothing verified, there is nothing to agree with: invalid."""
        result = validate_selectors(
            _FIVE_CARDS,
            LocatorSelectors(item="article.review-card"),
            [],
            min_agreement=0.8,
        )

        assert result.is_valid is False
        assert result.agreement == 0.0
