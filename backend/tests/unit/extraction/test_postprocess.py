"""Unit tests for post-processing (Task 4.2).

Covers turning a schema-validated :class:`~app.extraction.models.LocatorResult`
into Verified Reviews, implemented in ``app/extraction/postprocess.py``:
reading text/author/date/title from referenced elements with code, the rating
cue + range check, date parsing, and the discard-by-reason counting (unknown
refs, text shorter than 15 characters, duplicates, and excluded kinds).

A central invariant (steering: review text comes from page elements via code;
AI output may point at elements but may never supply review text) is exercised
directly: the Locator schema has no text field, and these tests assert the text
always comes from the resolved element, never from anything the AI could claim.

Property tests for Property 1 ("No invented text") and Property 6 ("Ratings in
range") over generated pages are Task 4.3 and live in ``tests/property/``; these
are focused example-based tests of each behavior.

_Validates: Requirements 2.2, 2.3, 2.4_
"""

from __future__ import annotations

from app.extraction.cleaner import build_clean_result
from app.extraction.models import LocatorItem, LocatorResult
from app.extraction.postprocess import postprocess

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean(html: str) -> tuple[str, dict[str, str]]:
    """Clean ``html`` and return the original HTML plus its ref lookup."""
    result = build_clean_result(html)
    return html, result.lookup


def _ref_for_text(html: str, lookup: dict[str, str], needle: str) -> str:
    """Return the ref of the smallest element whose text contains ``needle``.

    Preferring the shortest matching text picks the leaf field element (the
    ``<p>`` body) over an enclosing container (the ``<article>`` card) that also
    contains the substring, so each ref points at exactly the intended field.
    """
    from app.extraction.cleaner import resolve_ref

    best_ref: str | None = None
    best_len: int | None = None
    for ref in lookup:
        node = resolve_ref(html, lookup, ref)
        if node is None:
            continue
        text = node.text()
        if needle in text and (best_len is None or len(text) < best_len):
            best_ref = ref
            best_len = len(text)
    if best_ref is None:
        msg = f"no ref resolves to text containing {needle!r}"
        raise AssertionError(msg)
    return best_ref


# A single review card with block-level body, author, date, and title elements
# (only block tags get reference IDs from the cleaner, so fields live in block
# elements here).  ``aria-label`` on the rating element is a rating cue.
_REVIEW_CARD = (
    "<html><body>"
    '<article class="review-card">'
    '<h3 class="title">Loved the setup</h3>'
    '<p class="body">Great tool, the setup took an afternoon but it worked well overall.</p>'
    '<a class="rating" aria-label="4 out of 5 stars" href="#">rating</a>'
    '<p class="author">Dana Scully</p>'
    "<time>January 2, 2023</time>"
    "</article>"
    "</body></html>"
)


# ---------------------------------------------------------------------------
# Reading fields from refs (Requirement 2.2)
# ---------------------------------------------------------------------------


class TestReadFieldsFromRefs:
    """Text, author, date, and title are read with code from referenced elements."""

    def test_reads_all_fields_from_elements(self) -> None:
        html, lookup = _clean(_REVIEW_CARD)
        item = LocatorItem(
            item_ref=_ref_for_text(html, lookup, "Great tool"),
            text_ref=_ref_for_text(html, lookup, "Great tool"),
            author_ref=_ref_for_text(html, lookup, "Dana Scully"),
            date_ref=_ref_for_text(html, lookup, "January 2, 2023"),
            title_ref=_ref_for_text(html, lookup, "Loved the setup"),
            kind="review",
        )
        result = LocatorResult(has_reviews=True, rating_scale=5, items=[item])

        reviews, discarded = postprocess(html, lookup, result)

        assert discarded == {}
        assert len(reviews) == 1
        review = reviews[0]
        assert review.text == "Great tool, the setup took an afternoon but it worked well overall."
        assert review.author == "Dana Scully"
        assert review.title == "Loved the setup"
        assert review.date == "2023-01-02"
        assert review.source_ref == item.text_ref

    def test_text_read_from_item_when_text_ref_null(self) -> None:
        """A null ``text_ref`` falls back to reading the item element's text."""
        html = (
            "<html><body>"
            '<p class="review">A perfectly fine review body with enough length.</p>'
            "</body></html>"
        )
        html, lookup = _clean(html)
        body_ref = _ref_for_text(html, lookup, "perfectly fine")
        item = LocatorItem(item_ref=body_ref, text_ref=None, kind="review")
        result = LocatorResult(has_reviews=True, items=[item])

        reviews, discarded = postprocess(html, lookup, result)

        assert discarded == {}
        assert len(reviews) == 1
        assert reviews[0].text == "A perfectly fine review body with enough length."
        assert reviews[0].source_ref == body_ref

    def test_optional_fields_absent_are_none(self) -> None:
        html, lookup = _clean(_REVIEW_CARD)
        item = LocatorItem(
            item_ref=_ref_for_text(html, lookup, "Great tool"),
            text_ref=_ref_for_text(html, lookup, "Great tool"),
            kind="review",
        )
        result = LocatorResult(has_reviews=True, items=[item])

        reviews, _ = postprocess(html, lookup, result)

        assert reviews[0].author is None
        assert reviews[0].title is None
        assert reviews[0].date is None


# ---------------------------------------------------------------------------
# The AI can never supply review text (steering invariant)
# ---------------------------------------------------------------------------


class TestTextAlwaysFromElement:
    """Review text is always the element's text — the AI cannot inject text."""

    def test_text_matches_element_not_any_ai_field(self) -> None:
        """The schema has no text field; text equals the resolved element text.

        A Locator "that supplies its own text" has nowhere to put it: the item
        carries references only.  This seeds the Task 4.3 case and asserts the
        invariant directly — the kept text is exactly the page element's text.
        """
        html, lookup = _clean(_REVIEW_CARD)
        body_ref = _ref_for_text(html, lookup, "Great tool")
        item = LocatorItem(item_ref=body_ref, text_ref=body_ref, kind="review")
        result = LocatorResult(has_reviews=True, items=[item])

        reviews, _ = postprocess(html, lookup, result)

        # The LocatorItem model exposes no attribute that could carry text.
        assert not hasattr(item, "text")
        assert not hasattr(item, "review_text")
        # Text came from the element.
        from app.extraction.cleaner import resolve_ref

        node = resolve_ref(html, lookup, body_ref)
        assert node is not None
        assert reviews[0].text in node.text()


# ---------------------------------------------------------------------------
# Discards: unknown refs (Requirement 2.4)
# ---------------------------------------------------------------------------


class TestUnknownRefs:
    """Items whose references don't exist are discarded and counted."""

    def test_unknown_item_ref_discarded(self) -> None:
        html, lookup = _clean(_REVIEW_CARD)
        item = LocatorItem(item_ref="e9999", text_ref="e9999", kind="review")
        result = LocatorResult(has_reviews=True, items=[item])

        reviews, discarded = postprocess(html, lookup, result)

        assert reviews == []
        assert discarded == {"unknown_ref": 1}

    def test_unknown_field_ref_discarded(self) -> None:
        """A resolvable item but an unresolvable field ref is still discarded."""
        html, lookup = _clean(_REVIEW_CARD)
        item = LocatorItem(
            item_ref=_ref_for_text(html, lookup, "Great tool"),
            text_ref=_ref_for_text(html, lookup, "Great tool"),
            author_ref="e9999",
            kind="review",
        )
        result = LocatorResult(has_reviews=True, items=[item])

        reviews, discarded = postprocess(html, lookup, result)

        assert reviews == []
        assert discarded == {"unknown_ref": 1}


# ---------------------------------------------------------------------------
# Discards: short text (Requirement 2.4)
# ---------------------------------------------------------------------------


class TestTooShort:
    """Reviews whose text is shorter than 15 characters are discarded."""

    def test_short_text_discarded(self) -> None:
        html = "<html><body><p>Too short</p></body></html>"
        html, lookup = _clean(html)
        ref = _ref_for_text(html, lookup, "Too short")
        item = LocatorItem(item_ref=ref, text_ref=ref, kind="review")
        result = LocatorResult(has_reviews=True, items=[item])

        reviews, discarded = postprocess(html, lookup, result)

        assert reviews == []
        assert discarded == {"too_short": 1}

    def test_exactly_15_chars_kept(self) -> None:
        html = "<html><body><p>Fifteen chars!!</p></body></html>"
        html, lookup = _clean(html)
        ref = _ref_for_text(html, lookup, "Fifteen")
        item = LocatorItem(item_ref=ref, text_ref=ref, kind="review")
        result = LocatorResult(has_reviews=True, items=[item])

        reviews, discarded = postprocess(html, lookup, result)

        assert len(reviews) == 1
        assert discarded == {}


# ---------------------------------------------------------------------------
# Discards: duplicates (Requirement 2.4)
# ---------------------------------------------------------------------------


class TestDuplicates:
    """A review duplicating an already-kept one is discarded."""

    def test_duplicate_text_discarded(self) -> None:
        html = (
            "<html><body>"
            '<p class="a">This exact review body appears twice on the page here.</p>'
            '<p class="b">This exact review body appears twice on the page here.</p>'
            "</body></html>"
        )
        html, lookup = _clean(html)
        refs = list(lookup)
        items = [
            LocatorItem(item_ref=refs[0], text_ref=refs[0], kind="review"),
            LocatorItem(item_ref=refs[1], text_ref=refs[1], kind="review"),
        ]
        result = LocatorResult(has_reviews=True, items=items)

        reviews, discarded = postprocess(html, lookup, result)

        assert len(reviews) == 1
        assert discarded == {"duplicate": 1}

    def test_duplicate_is_case_insensitive(self) -> None:
        html = (
            "<html><body>"
            '<p class="a">Mixed Case Review Body That Is Long Enough.</p>'
            '<p class="b">mixed case review body that is long enough.</p>'
            "</body></html>"
        )
        html, lookup = _clean(html)
        refs = list(lookup)
        items = [
            LocatorItem(item_ref=refs[0], text_ref=refs[0], kind="review"),
            LocatorItem(item_ref=refs[1], text_ref=refs[1], kind="review"),
        ]
        result = LocatorResult(has_reviews=True, items=items)

        reviews, discarded = postprocess(html, lookup, result)

        assert len(reviews) == 1
        assert discarded == {"duplicate": 1}


# ---------------------------------------------------------------------------
# Discards: excluded kinds (Requirement 2.4)
# ---------------------------------------------------------------------------


class TestExcludedKinds:
    """Non-review kinds are discarded and counted by their kind."""

    def test_each_excluded_kind_counted_by_name(self) -> None:
        html = (
            "<html><body>"
            '<p class="qa">A question-and-answer entry long enough to pass length.</p>'
            '<p class="sr">A seller response entry long enough to pass the length check.</p>'
            '<p class="or">An owner response entry long enough to pass the length check.</p>'
            '<p class="ed">An editorial summary entry long enough to pass length check.</p>'
            '<p class="ad">An advertisement entry long enough to pass the length check.</p>'
            "</body></html>"
        )
        html, lookup = _clean(html)
        refs = list(lookup)
        kinds = ["qa", "seller_response", "owner_response", "editorial", "ad"]
        items = [
            LocatorItem(item_ref=ref, text_ref=ref, kind=kind)  # type: ignore[arg-type]
            for ref, kind in zip(refs, kinds, strict=True)
        ]
        result = LocatorResult(has_reviews=True, items=items)

        reviews, discarded = postprocess(html, lookup, result)

        assert reviews == []
        assert discarded == {
            "qa": 1,
            "seller_response": 1,
            "owner_response": 1,
            "editorial": 1,
            "ad": 1,
        }


# ---------------------------------------------------------------------------
# Rating cue + range (Requirement 2.3 / Property 6)
# ---------------------------------------------------------------------------


class TestRating:
    """A rating is kept only with a cue and within the page's rating scale."""

    def test_rating_kept_with_cue_in_range(self) -> None:
        html, lookup = _clean(_REVIEW_CARD)
        text_ref = _ref_for_text(html, lookup, "Great tool")
        rating_ref = _ref_for_text(html, lookup, "rating")  # has aria-label cue
        item = LocatorItem(
            item_ref=text_ref,
            text_ref=text_ref,
            rating_value=4,
            rating_ref=rating_ref,
            kind="review",
        )
        result = LocatorResult(has_reviews=True, rating_scale=5, items=[item])

        reviews, _ = postprocess(html, lookup, result)

        assert reviews[0].rating == 4

    def test_rating_dropped_without_cue(self) -> None:
        """A rating pointing at a cue-less element is dropped; the review stays."""
        html = (
            "<html><body>"
            '<p class="plain">A review body with enough length but no rating cue.</p>'
            "</body></html>"
        )
        html, lookup = _clean(html)
        ref = _ref_for_text(html, lookup, "review body")
        item = LocatorItem(
            item_ref=ref,
            text_ref=ref,
            rating_value=4,
            rating_ref=ref,  # no aria-label / rating class → no cue
            kind="review",
        )
        result = LocatorResult(has_reviews=True, rating_scale=5, items=[item])

        reviews, discarded = postprocess(html, lookup, result)

        assert len(reviews) == 1
        assert reviews[0].rating is None
        assert discarded == {}

    def test_rating_dropped_out_of_range(self) -> None:
        """An out-of-range rating is dropped even with a cue; the review stays."""
        html, lookup = _clean(_REVIEW_CARD)
        text_ref = _ref_for_text(html, lookup, "Great tool")
        rating_ref = _ref_for_text(html, lookup, "rating")
        item = LocatorItem(
            item_ref=text_ref,
            text_ref=text_ref,
            rating_value=7,  # above scale of 5
            rating_ref=rating_ref,
            kind="review",
        )
        result = LocatorResult(has_reviews=True, rating_scale=5, items=[item])

        reviews, _ = postprocess(html, lookup, result)

        assert len(reviews) == 1
        assert reviews[0].rating is None

    def test_rating_cue_on_item_when_no_rating_ref(self) -> None:
        """With no ``rating_ref``, the cue is checked on the item element."""
        html = (
            "<html><body>"
            '<a class="rating-row" aria-label="3 of 5" href="#">'
            "A review body long enough to be kept and carrying its rating cue."
            "</a>"
            "</body></html>"
        )
        html, lookup = _clean(html)
        ref = _ref_for_text(html, lookup, "review body")
        item = LocatorItem(
            item_ref=ref,
            text_ref=ref,
            rating_value=3,
            rating_ref=None,
            kind="review",
        )
        result = LocatorResult(has_reviews=True, rating_scale=5, items=[item])

        reviews, _ = postprocess(html, lookup, result)

        assert reviews[0].rating == 3


# ---------------------------------------------------------------------------
# Date parsing (Requirement 2.2)
# ---------------------------------------------------------------------------


class TestDateParsing:
    """Unambiguous dates become ISO; unparseable text is kept raw."""

    def test_unambiguous_date_to_iso(self) -> None:
        html = (
            "<html><body>"
            '<p class="b">A review body with enough length to be kept here.</p>'
            "<time>March 5, 2021</time>"
            "</body></html>"
        )
        html, lookup = _clean(html)
        body_ref = _ref_for_text(html, lookup, "review body")
        date_ref = _ref_for_text(html, lookup, "March 5")
        item = LocatorItem(item_ref=body_ref, text_ref=body_ref, date_ref=date_ref, kind="review")
        result = LocatorResult(has_reviews=True, items=[item])

        reviews, _ = postprocess(html, lookup, result)

        assert reviews[0].date == "2021-03-05"

    def test_garbage_date_kept_raw(self) -> None:
        html = (
            "<html><body>"
            '<p class="b">A review body with enough length to be kept here.</p>'
            "<time>not a date at all</time>"
            "</body></html>"
        )
        html, lookup = _clean(html)
        body_ref = _ref_for_text(html, lookup, "review body")
        date_ref = _ref_for_text(html, lookup, "not a date")
        item = LocatorItem(item_ref=body_ref, text_ref=body_ref, date_ref=date_ref, kind="review")
        result = LocatorResult(has_reviews=True, items=[item])

        reviews, _ = postprocess(html, lookup, result)

        assert reviews[0].date == "not a date at all"


# ---------------------------------------------------------------------------
# Mixed page: counts aggregate correctly, order preserved
# ---------------------------------------------------------------------------


class TestMixedPage:
    """A page mixing kept reviews and several discard reasons aggregates right."""

    def test_counts_and_order(self) -> None:
        html = (
            "<html><body>"
            '<p class="r1">First genuine review, plenty long to be kept here.</p>'
            '<p class="qa">A seller response that should be excluded by kind.</p>'
            '<p class="short">tiny</p>'
            '<p class="r2">Second genuine review, also long enough to keep.</p>'
            "</body></html>"
        )
        html, lookup = _clean(html)
        r1 = _ref_for_text(html, lookup, "First genuine")
        qa = _ref_for_text(html, lookup, "seller response")
        short = _ref_for_text(html, lookup, "tiny")
        r2 = _ref_for_text(html, lookup, "Second genuine")
        items = [
            LocatorItem(item_ref=r1, text_ref=r1, kind="review"),
            LocatorItem(item_ref=qa, text_ref=qa, kind="seller_response"),
            LocatorItem(item_ref=short, text_ref=short, kind="review"),
            LocatorItem(item_ref=r2, text_ref=r2, kind="review"),
        ]
        result = LocatorResult(has_reviews=True, items=items)

        reviews, discarded = postprocess(html, lookup, result)

        assert [r.text for r in reviews] == [
            "First genuine review, plenty long to be kept here.",
            "Second genuine review, also long enough to keep.",
        ]
        assert discarded == {"seller_response": 1, "too_short": 1}
