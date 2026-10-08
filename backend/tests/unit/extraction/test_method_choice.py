"""Unit tests for the method-choice decision table (Task 5.3, Requirement 4.2).

Covers :func:`app.extraction.plan._choose_method`, the pure helper that decides
which extraction method a site gets, and :func:`app.extraction.plan._texts_agree`,
the deterministic overlap check that feeds it.

The method-choice rule (Requirement 4.2) is a small truth table over four
inputs:

- ``selector_valid`` — did the suggested selectors validate?
- ``structured_count`` — how many reviews Structured Review Data holds.
- ``locator_found`` — how many Verified Reviews the Locator produced.
- ``texts_agree`` — do the structured and Locator texts overlap enough?

The rule:

1. ``selectors`` whenever the selectors validated (regardless of everything else);
2. else ``structured`` when ``structured_count > locator_found`` **and**
   ``texts_agree``;
3. else ``ai_direct``.

The wiring of these inputs into :func:`build_plan` is covered by Task 5.2's
tests in ``test_plan.py``; here we table-test the pure decision in isolation so
every branch — including the "equal counts" and "fewer counts" edges — is
exercised directly.

_Validates: Requirements 4.2_
"""

from __future__ import annotations

import pytest
from app.extraction.models import VerifiedReview
from app.extraction.plan import _choose_method, _texts_agree

# ---------------------------------------------------------------------------
# Method-choice decision table (Requirement 4.2)
# ---------------------------------------------------------------------------


class TestChooseMethodSelectorsWin:
    """Rule 1: valid selectors always win, regardless of the other inputs."""

    @pytest.mark.parametrize("structured_count", [0, 3, 10])
    @pytest.mark.parametrize("locator_found", [0, 3, 10])
    @pytest.mark.parametrize("texts_agree", [True, False])
    def test_valid_selectors_always_choose_selectors(
        self,
        structured_count: int,
        locator_found: int,
        texts_agree: bool,
    ) -> None:
        """``selector_valid`` True → ``selectors`` for every other combination."""
        assert (
            _choose_method(
                selector_valid=True,
                structured_count=structured_count,
                locator_found=locator_found,
                texts_agree=texts_agree,
            )
            == "selectors"
        )


class TestChooseMethodStructuredWins:
    """Rule 2: no valid selectors, structured holds *more* and texts agree."""

    def test_structured_strictly_more_and_agreeing_chooses_structured(self) -> None:
        assert (
            _choose_method(
                selector_valid=False,
                structured_count=5,
                locator_found=2,
                texts_agree=True,
            )
            == "structured"
        )

    def test_structured_one_more_and_agreeing_chooses_structured(self) -> None:
        """The boundary: a single extra structured review still counts as more."""
        assert (
            _choose_method(
                selector_valid=False,
                structured_count=3,
                locator_found=2,
                texts_agree=True,
            )
            == "structured"
        )


class TestChooseMethodAiDirectFallback:
    """Rule 3: everything else falls back to ``ai_direct``."""

    def test_more_structured_but_texts_disagree_chooses_ai_direct(self) -> None:
        """More structured reviews, but they are a different set → ``ai_direct``."""
        assert (
            _choose_method(
                selector_valid=False,
                structured_count=5,
                locator_found=2,
                texts_agree=False,
            )
            == "ai_direct"
        )

    @pytest.mark.parametrize("texts_agree", [True, False])
    def test_equal_counts_chooses_ai_direct(self, texts_agree: bool) -> None:
        """``structured_count == locator_found`` is not *more* → ``ai_direct``.

        Agreement is irrelevant here: the "more" clause already fails.
        """
        assert (
            _choose_method(
                selector_valid=False,
                structured_count=3,
                locator_found=3,
                texts_agree=texts_agree,
            )
            == "ai_direct"
        )

    @pytest.mark.parametrize("texts_agree", [True, False])
    def test_fewer_structured_chooses_ai_direct(self, texts_agree: bool) -> None:
        """Structured holds *fewer* than the Locator → ``ai_direct``."""
        assert (
            _choose_method(
                selector_valid=False,
                structured_count=1,
                locator_found=4,
                texts_agree=texts_agree,
            )
            == "ai_direct"
        )

    def test_no_structured_data_chooses_ai_direct(self) -> None:
        """No structured reviews at all → ``ai_direct`` (the common case)."""
        assert (
            _choose_method(
                selector_valid=False,
                structured_count=0,
                locator_found=3,
                texts_agree=False,
            )
            == "ai_direct"
        )

    def test_nothing_found_anywhere_chooses_ai_direct(self) -> None:
        """All zero inputs → ``ai_direct`` (equal counts, so not "more")."""
        assert (
            _choose_method(
                selector_valid=False,
                structured_count=0,
                locator_found=0,
                texts_agree=False,
            )
            == "ai_direct"
        )


# ---------------------------------------------------------------------------
# ``_texts_agree`` — the overlap check feeding the "texts agree" input
# ---------------------------------------------------------------------------


def _reviews(*texts: str) -> list[VerifiedReview]:
    """Build Verified Reviews from their body texts."""
    return [VerifiedReview(text=text) for text in texts]


class TestTextsAgree:
    """Deterministic overlap between structured and Locator review texts."""

    def test_identical_sets_agree(self) -> None:
        texts = (
            "The onboarding was smooth and support replied the same day.",
            "Dashboard exports are clean but the mobile app lags sometimes.",
        )
        assert _texts_agree(_reviews(*texts), _reviews(*texts)) is True

    def test_fully_disjoint_sets_do_not_agree(self) -> None:
        structured = _reviews(
            "Completely unrelated structured review body about shipping speed.",
            "Another structured-only body describing packaging quality in detail.",
        )
        verified = _reviews(
            "A verified review that shares no words of substance with the above.",
            "Yet another verified body talking only about the mobile experience.",
        )
        assert _texts_agree(structured, verified) is False

    def test_empty_structured_set_does_not_agree(self) -> None:
        """Nothing to compare on one side → no agreement."""
        assert _texts_agree([], _reviews("Some verified review body text here.")) is False

    def test_empty_verified_set_does_not_agree(self) -> None:
        assert _texts_agree(_reviews("Some structured review body text here."), []) is False

    def test_both_empty_do_not_agree(self) -> None:
        assert _texts_agree([], []) is False

    def test_containment_matching_counts_as_agreement(self) -> None:
        """Structured bodies may be longer than the verified field text.

        A verified review "matches" a structured one when either normalized
        text contains the other, so trimmed field text still agrees with a
        fuller structured body.
        """
        structured = _reviews(
            "Great tool, the setup took an afternoon but it worked well overall. "
            "Signed, a happy reviewer.",
            "Support was responsive and fixed my issue within a single day flat.",
        )
        verified = _reviews(
            "Great tool, the setup took an afternoon but it worked well overall.",
            "Support was responsive and fixed my issue within a single day flat.",
        )
        assert _texts_agree(structured, verified) is True

    def test_majority_overlap_agrees(self) -> None:
        """At least half the smaller set matching is enough to agree."""
        shared = (
            "The reports export cleanly to CSV and the API is well documented.",
            "Pricing is fair for a small team and onboarding was quite painless.",
        )
        structured = _reviews(*shared)
        verified = _reviews(
            *shared,
            "An extra verified-only body that appears nowhere in structured data.",
        )
        # Smaller set is the structured pair; both match → overlap 1.0 ≥ 0.5.
        assert _texts_agree(structured, verified) is True

    def test_minority_overlap_does_not_agree(self) -> None:
        """Below-half overlap of the equal-sized smaller set → disagreement.

        Both sets have four reviews, so the smaller set is four.  Only one pair
        matches, giving overlap 1/4 < 0.5, so the sets do not agree.
        """
        shared = "The reports export cleanly to CSV and the API is well documented."
        structured = _reviews(
            shared,
            "A structured-only body one that shares nothing with verified texts.",
            "A structured-only body two that shares nothing with verified texts.",
            "A structured-only body three that shares nothing with verified text.",
        )
        verified = _reviews(
            shared,
            "A verified-only body one that appears nowhere within structured set.",
            "A verified-only body two that appears nowhere within structured set.",
            "A verified-only body three that appears nowhere within structured set.",
        )
        assert _texts_agree(structured, verified) is False

    def test_exactly_half_overlap_agrees(self) -> None:
        """Exactly half the equal-sized smaller set matching reaches the bar.

        Two of four pairs match → overlap 2/4 = 0.5, which satisfies the
        ``>= 0.5`` threshold, so the sets agree.
        """
        shared = (
            "The reports export cleanly to CSV and the API is well documented.",
            "Pricing is fair for a small team and onboarding was quite painless.",
        )
        structured = _reviews(
            *shared,
            "A structured-only body one that shares nothing with verified texts.",
            "A structured-only body two that shares nothing with verified texts.",
        )
        verified = _reviews(
            *shared,
            "A verified-only body one that appears nowhere within structured set.",
            "A verified-only body two that appears nowhere within structured set.",
        )
        assert _texts_agree(structured, verified) is True
