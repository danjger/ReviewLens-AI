"""Unit tests for app.handlers.dedupe (review-analysis task 3.3).

These cover the cross-page dedupe stage: it removes duplicate reviews by
normalized text plus author plus date, keeps the first (earliest-page)
occurrence, preserves order, and is idempotent. The Hypothesis property test
for Property 1 is deferred to task 3.4.

_Validates: Requirement 3.3_
"""

from __future__ import annotations

from app.extraction.models import VerifiedReview
from app.handlers.dedupe import dedupe
from app.handlers.extraction_stage import CollectedReview


def _review(
    text: str,
    *,
    author: str | None = None,
    date: str | None = None,
    page: int = 1,
) -> CollectedReview:
    return CollectedReview(
        review=VerifiedReview(text=text, author=author, date=date),
        source_page=page,
    )


def _texts(reviews: list[CollectedReview]) -> list[str]:
    return [c.review.text for c in reviews]


def test_exact_duplicate_removed() -> None:
    """Two identical reviews collapse to one, keeping the first."""
    reviews = [
        _review("Great product", author="Alice", date="2026-01-01"),
        _review("Great product", author="Alice", date="2026-01-01"),
    ]
    result = dedupe(reviews)
    assert len(result) == 1
    assert result[0] is reviews[0]


def test_duplicate_across_pages_keeps_earliest_page() -> None:
    """A duplicate spanning pages is removed; the lowest source_page is kept."""
    reviews = [
        _review("Loved it", author="Bob", date="2026-02-02", page=1),
        _review("Something else", author="Carol", date="2026-02-03", page=1),
        _review("Loved it", author="Bob", date="2026-02-02", page=2),
    ]
    result = dedupe(reviews)
    assert len(result) == 2
    assert _texts(result) == ["Loved it", "Something else"]
    # The page-1 copy survives (earliest page wins).
    assert result[0].source_page == 1


def test_differ_only_by_author_not_merged() -> None:
    """Same text and date but different authors are distinct reviews."""
    reviews = [
        _review("Nice", author="Alice", date="2026-01-01"),
        _review("Nice", author="Bob", date="2026-01-01"),
    ]
    result = dedupe(reviews)
    assert len(result) == 2


def test_differ_only_by_date_not_merged() -> None:
    """Same text and author but different dates are distinct reviews."""
    reviews = [
        _review("Nice", author="Alice", date="2026-01-01"),
        _review("Nice", author="Alice", date="2026-02-01"),
    ]
    result = dedupe(reviews)
    assert len(result) == 2


def test_missing_author_distinct_from_present_author() -> None:
    """A review with no author does not match one that names an author."""
    reviews = [
        _review("Hello", author=None, date="2026-01-01"),
        _review("Hello", author="Alice", date="2026-01-01"),
    ]
    result = dedupe(reviews)
    assert len(result) == 2


def test_missing_optional_fields_collapse_together() -> None:
    """Reviews with the same text and no author/date collapse to one."""
    reviews = [
        _review("Same text", author=None, date=None),
        _review("Same text", author=None, date=None),
    ]
    result = dedupe(reviews)
    assert len(result) == 1


def test_normalization_whitespace_and_case() -> None:
    """Whitespace and case differences are treated as the same text."""
    reviews = [
        _review("Great   Product", author="Alice", date="2026-01-01"),
        _review("great product", author="ALICE", date="2026-01-01"),
    ]
    result = dedupe(reviews)
    assert len(result) == 1


def test_normalization_applies_to_author_and_date() -> None:
    """Author and date are normalized too (whitespace/case)."""
    reviews = [
        _review("Text", author="J.  Doe", date="2026-01-01"),
        _review("Text", author="j. doe", date="2026-01-01"),
    ]
    result = dedupe(reviews)
    assert len(result) == 1


def test_order_preserved() -> None:
    """Kept reviews keep their input order."""
    reviews = [
        _review("A"),
        _review("B"),
        _review("C"),
        _review("A"),  # dup of first
        _review("D"),
    ]
    result = dedupe(reviews)
    assert _texts(result) == ["A", "B", "C", "D"]


def test_idempotent() -> None:
    """dedupe(dedupe(x)) == dedupe(x) (design Property 1)."""
    reviews = [
        _review("A", author="x", date="d1"),
        _review("B", author="y", date="d2"),
        _review("A", author="x", date="d1"),
        _review("B", author="y", date="d2"),
        _review("C"),
    ]
    once = dedupe(reviews)
    twice = dedupe(once)
    assert _texts(twice) == _texts(once)
    assert twice == once


def test_empty_input() -> None:
    """Deduping an empty list returns an empty list."""
    assert dedupe([]) == []


def test_no_duplicates_returns_all() -> None:
    """A list with no duplicates is returned intact and in order."""
    reviews = [_review("A"), _review("B"), _review("C")]
    result = dedupe(reviews)
    assert _texts(result) == ["A", "B", "C"]
