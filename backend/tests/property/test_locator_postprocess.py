"""Property-based tests for Locator post-processing (Task 4.3).

Covers two of the Extraction Engine's correctness properties from
``.kiro/specs/review-extraction/design.md``:

- Property 1: No invented text (Requirements 2.2, 3.2).
- Property 6: Ratings in range (Requirement 2.3).

Both drive the real :func:`app.extraction.postprocess.postprocess` over
generated HTML review lists and generated :class:`LocatorResult` responses.  The
HTML generator reuses the review-list style from ``test_cleaner.py`` (random
layouts, rating attributes, authors, and noise); the cleaner builds the ref
lookup, and the generated Locator items point at real refs (so some survive) and
occasionally at non-existent refs (which post-processing must discard).  No AI
is called — the suite-wide autouse fixture points the client at the offline
``FakeClaude`` stub — so these tests are deterministic and network-free.
"""

from __future__ import annotations

import re

from app.extraction.cleaner import build_clean_result
from app.extraction.models import LocatorItem, LocatorResult
from app.extraction.postprocess import postprocess
from hypothesis import given
from hypothesis import strategies as st
from selectolax.parser import HTMLParser

# Mirror the cleaner's whitespace normalization so "normalized visible text" is
# computed the same way post-processing computes a review's text.
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Collapse whitespace runs to single spaces and strip (cleaner's rule)."""
    return _WS_RE.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# Leaf-content strategies (kept markup-safe: no <, >, or quotes).
# ---------------------------------------------------------------------------

_visible_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters=" .,!",
    ),
    min_size=20,  # comfortably over the 15-char keep threshold
    max_size=160,
)

_author_text = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll"), whitelist_characters=" "),
    min_size=3,
    max_size=24,
)

_rating_scale = st.sampled_from([5, 10, 100])


@st.composite
def _review_card(draw: st.DrawFn, scale: int) -> str:
    """Draw one visible review card; sometimes with an explicit rating cue."""
    body = draw(_visible_text)
    author = draw(_author_text)
    tag = draw(st.sampled_from(["article", "div", "li", "section"]))
    rating_attr = ""
    if draw(st.booleans()):
        stars = draw(st.integers(min_value=1, max_value=scale))
        rating_attr = f' aria-label="{stars} out of {scale} stars"'
    inner = f'<p class="body">{body}</p><span class="author">{author}</span>'
    return f'<{tag} class="review"{rating_attr}>{inner}</{tag}>'


@st.composite
def _generated_review_page(draw: st.DrawFn) -> tuple[str, int]:
    """Draw an HTML review-list page and the rating scale it was built with."""
    scale = draw(_rating_scale)
    n = draw(st.integers(min_value=1, max_value=6))
    cards = "".join(draw(_review_card(scale)) for _ in range(n))
    # Optional surrounding boilerplate and a wrapper, as in the cleaner suite.
    header = "<header>Logo Home About</header>" if draw(st.booleans()) else ""
    footer = "<footer>Terms Privacy</footer>" if draw(st.booleans()) else ""
    body = f"{header}<main>{cards}</main>{footer}"
    return f"<html><body>{body}</body></html>", scale


# ---------------------------------------------------------------------------
# Build a LocatorResult over a cleaned page: real refs (so some survive) plus
# some non-existent refs (which post-processing must discard).
# ---------------------------------------------------------------------------


@st.composite
def _locator_result_for(
    draw: st.DrawFn,
    lookup: dict[str, str],
    *,
    rating_scale: int,
) -> LocatorResult:
    """Draw a LocatorResult whose items point at a page's refs and some fakes."""
    real_refs = list(lookup)
    items: list[LocatorItem] = []

    # Some items point at genuine refs; each gets a random rating value that may
    # be in or out of range, so Property 6's range filter is exercised.
    for ref in draw(st.lists(st.sampled_from(real_refs or ["e1"]), min_size=0, max_size=6)):
        rating_value = draw(
            st.one_of(
                st.none(),
                st.integers(min_value=-3, max_value=rating_scale + 5),
                st.floats(min_value=-3, max_value=rating_scale + 5, allow_nan=False),
            )
        )
        items.append(
            LocatorItem(
                item_ref=ref,
                text_ref=ref,
                rating_value=rating_value,
                rating_ref=ref,  # the card carries the aria-label cue when present
                kind="review",
            )
        )

    # A few items point at refs that do not exist — must be discarded, never kept.
    for fake in draw(st.lists(st.text(alphabet="e0123456789", min_size=2, max_size=6))):
        if fake not in lookup:
            items.append(LocatorItem(item_ref=fake, text_ref=fake, kind="review"))

    return LocatorResult(
        has_reviews=bool(items),
        rating_scale=rating_scale,
        items=items,
    )


# ---------------------------------------------------------------------------
# Property 1: No invented text (Requirements 2.2, 3.2)
# ---------------------------------------------------------------------------


@given(data=st.data())
def test_no_invented_text(data: st.DataObject) -> None:
    """Property 1: No invented text.

    For any page and any Locator response, every Verified Review's text SHALL
    be a substring of the page's normalized visible text.
    Validates: Requirements 2.2, 3.2
    """
    html, scale = data.draw(_generated_review_page())
    cleaned = build_clean_result(html)
    result = data.draw(_locator_result_for(cleaned.lookup, rating_scale=scale))

    reviews, _discarded = postprocess(html, cleaned.lookup, result)

    # The page's normalized visible text: the body's text, whitespace-collapsed
    # the same way post-processing normalizes each element's text.  Collapsing a
    # substring yields a substring of the collapsed whole, so a review read from
    # any element must appear here — unless text was invented.
    tree = HTMLParser(html)
    page_text = _normalize(tree.body.text()) if tree.body is not None else ""
    for review in reviews:
        assert review.text in page_text, (
            f"review text {review.text!r} is not a substring of the page's visible text"
        )


# ---------------------------------------------------------------------------
# Property 6: Ratings in range (Requirement 2.3)
# ---------------------------------------------------------------------------


@given(data=st.data())
def test_ratings_in_range(data: st.DataObject) -> None:
    """Property 6: Ratings in range.

    For any Locator response, every kept rating SHALL satisfy
    ``1 <= rating <= rating_scale``.
    Validates: Requirement 2.3
    """
    html, scale = data.draw(_generated_review_page())
    cleaned = build_clean_result(html)
    result = data.draw(_locator_result_for(cleaned.lookup, rating_scale=scale))

    reviews, _discarded = postprocess(html, cleaned.lookup, result)

    for review in reviews:
        if review.rating is not None:
            assert 1 <= review.rating <= scale, f"kept rating {review.rating} outside 1..{scale}"


# ---------------------------------------------------------------------------
# A focused regression: a non-existent ref is never turned into a review
# (keeps Property 1 honest even when a page has no cards at all).
# ---------------------------------------------------------------------------


@given(fake_ref=st.text(alphabet="e0123456789", min_size=2, max_size=6))
def test_unknown_ref_never_becomes_a_review(fake_ref: str) -> None:
    """Property 1 corollary: an unresolved ref yields no text, so no review.

    Validates: Requirements 2.2, 3.2
    """
    html = '<html><body><main><p class="body">A real review body here.</p></main></body></html>'
    cleaned = build_clean_result(html)
    # Point only at a ref that does not exist in the lookup.
    if fake_ref in cleaned.lookup:
        return
    result = LocatorResult(
        has_reviews=True,
        items=[LocatorItem(item_ref=fake_ref, text_ref=fake_ref, kind="review")],
    )

    reviews, discarded = postprocess(html, cleaned.lookup, result)

    assert reviews == []
    assert discarded == {"unknown_ref": 1}
