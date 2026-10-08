"""Property-based test for selector validation (Task 5.3).

Covers Property 8 from ``.kiro/specs/review-extraction/design.md``:

- **Property 8: Selector validation is sound.** *For any* generated review
  list, selectors marked valid SHALL reproduce at least the configured share of
  verified texts (Requirement 4.1).

The strategy generates HTML review lists with a stable wrapper class and a body
sub-element (the style reused from ``test_cleaner.py``), interleaved with noise
cards and sometimes verified texts that do not appear on the page.  It then
feeds a generated ``item`` selector (the real wrapper class, a broad selector,
or a selector that matches nothing/everything) and a generated ``min_agreement``
in ``(0, 1]`` to :func:`app.extraction.selectors.validate_selectors`, and asserts
the soundness guarantees whenever the result is marked valid.

:func:`validate_selectors` is pure and AI-free, so no AI stub is needed; the
suite-wide autouse ``FakeClaude`` fixture keeps any incidental token counting
offline anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.extraction.cleaner import _WS_RE
from app.extraction.models import LocatorSelectors, VerifiedReview
from app.extraction.selectors import (
    SELECTOR_MAX_OVER_SELECTION,
    validate_selectors,
)
from hypothesis import given
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Generated review list
# ---------------------------------------------------------------------------

#: Stable wrapper class the review cards carry — the selector a well-behaved
#: Locator would suggest.
_CARD_CLASS = "review-card"

# Visible review-ish text: letters, digits, and a little punctuation, kept free
# of ``<``/``>``/quotes so the generated markup stays well-formed.  A healthy
# minimum size keeps distinct bodies from colliding as substrings of each other.
_body_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters=" .,!",
    ),
    min_size=20,
    max_size=120,
)


@dataclass
class GeneratedReviewList:
    """A generated page plus the Verified Reviews to validate selectors against."""

    html: str
    verified: list[VerifiedReview] = field(default_factory=list)


def _normalize(text: str) -> str:
    """Collapse whitespace exactly as the validator does, for the oracle below."""
    return _WS_RE.sub(" ", text).strip()


@st.composite
def _review_list(draw: st.DrawFn) -> GeneratedReviewList:
    """Draw a review-list page and the Verified Reviews read from it.

    The page has a run of review cards (``article.review-card`` wrapping a body
    paragraph plus an author line, so the wrapper text contains but does not
    equal the body) and optionally some noise cards sharing a broader class.
    Verified Reviews are built from a random subset of the real card bodies and,
    sometimes, a few texts that never appear on the page — so agreement is
    genuinely partial across the generated space.
    """
    n_cards = draw(st.integers(min_value=1, max_value=8))
    # Distinct bodies so one body cannot accidentally be a substring of another.
    bodies = draw(st.lists(_body_text, min_size=n_cards, max_size=n_cards, unique_by=_normalize))

    cards = "".join(
        f'<article class="{_CARD_CLASS}">'
        f'<p class="body">{body}</p>'
        f'<span class="author">Reviewer {i}</span>'
        f"</article>"
        for i, body in enumerate(bodies)
    )

    # Optional noise cards that share the broader ``card`` class but not the
    # review wrapper class, so a broad ``div.card`` selector over-selects.
    n_noise = draw(st.integers(min_value=0, max_value=5))
    noise = "".join(
        f'<div class="card">Unrelated promotional block number {i} here.</div>'
        for i in range(n_noise)
    )

    html = f"<html><body><main>{cards}{noise}</main></body></html>"

    # Verified Reviews: a non-empty subset of real bodies, plus 0–3 off-page
    # texts that no selected item can reproduce.
    on_page = draw(st.lists(st.sampled_from(bodies), min_size=1, max_size=n_cards, unique=True))
    n_off_page = draw(st.integers(min_value=0, max_value=3))
    off_page = [
        f"This verified review body number {i} never appears on the page at all."
        for i in range(n_off_page)
    ]
    verified = [VerifiedReview(text=t) for t in (*on_page, *off_page)]
    return GeneratedReviewList(html=html, verified=verified)


# Generated ``item`` selectors spanning the interesting cases: the exact review
# wrapper, the broad card class (matches review cards *and* noise), a selector
# that matches nothing, and one that matches everything.
_item_selector = st.sampled_from(
    [
        f"article.{_CARD_CLASS}",
        "div.card",
        ".card",
        "article",
        "section.absent-does-not-exist",
        "*",
    ]
)

# A configured agreement share in (0, 1].
_min_agreement = st.floats(
    min_value=0.01,
    max_value=1.0,
    allow_nan=False,
    allow_infinity=False,
)


# ---------------------------------------------------------------------------
# Property 8: Selector validation is sound
# ---------------------------------------------------------------------------


@given(
    data=_review_list(),
    item_selector=_item_selector,
    min_agreement=_min_agreement,
)
def test_valid_selectors_are_sound(
    data: GeneratedReviewList,
    item_selector: str,
    min_agreement: float,
) -> None:
    """Property 8: Selector validation is sound.

    For any generated review list, selectors marked valid SHALL reproduce at
    least the configured share of verified texts.
    Validates: Requirement 4.1

    Whenever :func:`validate_selectors` reports ``is_valid``:

    - the measured ``agreement`` is at least ``min_agreement`` (the configured
      share), and
    - ``over_selection`` is within :data:`SELECTOR_MAX_OVER_SELECTION`, and
    - operationally, the selected items reproduce at least
      ``min_agreement * len(verified)`` of the Verified Reviews' texts.
    """
    result = validate_selectors(
        data.html,
        LocatorSelectors(item=item_selector),
        data.verified,
        min_agreement=min_agreement,
    )

    if not result.is_valid:
        return

    # The two threshold guarantees the validity verdict is built on.
    assert result.agreement >= min_agreement
    assert result.over_selection <= SELECTOR_MAX_OVER_SELECTION

    # Operational form: a valid selector reproduces at least the configured
    # share of verified texts.  ``matched_verified`` is the raw count behind the
    # agreement ratio; it must cover at least ``min_agreement`` of the verified
    # set.  Compared against the share directly (not a ``ceil`` count) so the
    # assertion is not fragile to floating-point rounding of the product.
    assert result.matched_verified >= min_agreement * len(data.verified)
