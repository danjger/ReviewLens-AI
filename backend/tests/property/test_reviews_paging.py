"""Property-based test for the reviews table paging (ingestion-summary task 9).

Property 1: Paging returns every match once.
  For any reviews file, filter combination, and page size, concatenating all
  pages SHALL equal the filtered list, with no duplicates and a stable order.
  Validates: Requirements 5.1, 5.2

This drives the **real** reviews helpers from
``app.datasets.summary`` — :func:`filter_reviews` (rating/sentiment/text-search
filter) and :func:`paginate` (1-based server-side slicing) — which are the pure
core of ``GET /datasets/{id}/reviews`` (design "API endpoints"). No S3 or
database is touched: both helpers are pure functions of their inputs, so the
property exercises them directly over Hypothesis-generated review sets, filter
combinations, and page sizes.

The claim has three parts, all of which the detail page's pager relies on:

- **Completeness + no duplicates.** Walking every page of the filtered list
  (page 1, 2, … until a page comes back short/empty) and concatenating the
  slices reproduces the filtered list exactly — same items, same count, so no
  match is dropped and none is served twice.
- **Stable order.** The concatenated pages preserve the filtered list's order,
  which is the stored order of the reviews file (filtering keeps order), so a
  review never jumps pages between requests.
- **The filtered list is itself a faithful, order-preserving subset.** Every
  item in it is an original review that passes the filters, in the original
  order — the baseline the pager pages over.
"""

from __future__ import annotations

from typing import Any

from app.datasets.summary import filter_reviews, paginate
from hypothesis import given
from hypothesis import strategies as st

# A small, closed vocabulary so filters actually match a meaningful fraction of
# the generated set (rather than almost never), exercising partial-match pages.
_SENTIMENTS = ["positive", "neutral", "negative"]
_WORDS = ["great", "awful", "fine", "okay", "love", "hate"]


@st.composite
def _review(draw: st.DrawFn) -> dict[str, Any]:
    """One review dict with the fields the filters read, plus a unique ``id``.

    ``rating`` spans the 1–5 stars and ``None`` (unrated); ``sentiment`` spans
    the three labels and a missing key; ``text`` is drawn from a small word pool
    so the substring search matches a non-trivial fraction. The ``id`` is
    assigned by the set builder so identity-based duplicate checks are exact.
    """
    rating = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=5)))
    sentiment = draw(st.one_of(st.none(), st.sampled_from(_SENTIMENTS)))
    words = draw(st.lists(st.sampled_from(_WORDS), min_size=0, max_size=4))
    review: dict[str, Any] = {"text": " ".join(words), "rating": rating}
    if sentiment is not None:
        review["sentiment"] = sentiment
    return review


@st.composite
def _case(draw: st.DrawFn) -> dict[str, Any]:
    """A reviews set, a filter combination, and a page size.

    Each review is tagged with a sequential ``id`` so the test can assert on
    identity (every match appears exactly once) independent of value equality.
    Filters are each optionally set; ``page_size`` ranges from 1 (every item its
    own page — the tightest boundary) up past the set size (a single page).
    """
    reviews = draw(st.lists(_review(), min_size=0, max_size=40))
    for index, review in enumerate(reviews):
        review["id"] = index

    rating = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=5)))
    sentiment = draw(st.one_of(st.none(), st.sampled_from(_SENTIMENTS)))
    query = draw(st.one_of(st.none(), st.sampled_from(_WORDS), st.just("zzz")))
    page_size = draw(st.integers(min_value=1, max_value=50))
    return {
        "reviews": reviews,
        "rating": rating,
        "sentiment": sentiment,
        "query": query,
        "page_size": page_size,
    }


@given(case=_case())
def test_paging_returns_every_match_once(case: dict[str, Any]) -> None:
    """Property 1: Paging returns every match once.

    Concatenating all pages of the filtered list equals the filtered list, with
    no duplicates and a stable (stored) order.
    Validates: Requirements 5.1, 5.2
    """
    reviews = case["reviews"]
    rating = case["rating"]
    sentiment = case["sentiment"]
    query = case["query"]
    page_size = case["page_size"]

    filtered = filter_reviews(reviews, rating=rating, sentiment=sentiment, query=query)

    # The filtered list is itself a faithful, order-preserving subset: every
    # item is an original review that passes the filters, kept in order.
    filtered_ids = [r["id"] for r in filtered]
    assert filtered_ids == [
        r["id"]
        for r in reviews
        if (rating is None or r.get("rating") == rating)
        and (
            sentiment is None
            or (isinstance(r.get("sentiment"), str) and r["sentiment"].lower() == sentiment.lower())
        )
        and (not query or (isinstance(r.get("text"), str) and query.lower() in r["text"].lower()))
    ]

    # Walk every page until a short/empty page signals the end, concatenating
    # the slices. One extra page past the end must come back empty (the pager
    # can over-request without error).
    collected: list[dict[str, Any]] = []
    page = 1
    while True:
        chunk = paginate(filtered, page=page, page_size=page_size)
        if not chunk:
            break
        # No page is longer than page_size.
        assert len(chunk) <= page_size
        collected.extend(chunk)
        page += 1

    # Completeness + no duplicates + stable order: the concatenation IS the
    # filtered list — same items, same count, same order.
    assert collected == filtered
    collected_ids = [r["id"] for r in collected]
    assert collected_ids == filtered_ids
    assert len(collected_ids) == len(set(collected_ids))
