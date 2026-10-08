"""Cross-page dedupe stage of the processing pipeline (review-analysis task 3.3).

After the per-page extraction stage (task 3.1) and the upload extraction stage
(task 3.2) have each produced a combined list of
:class:`~app.handlers.extraction_stage.CollectedReview` objects, both branches
feed this stage (design flowchart node ``D``). It removes duplicate reviews by
normalized text plus author plus date, including duplicates across pages
(Requirement 3.3).

Dedupe key (Requirement 3.3, design Correctness Property 1 — "no two reviews
with the same normalized text, author, and date"):

    (normalized text, normalized author, normalized date)

Normalization (:func:`_normalize_field`) matches the convention used across the
extraction package (see ``app.extraction.structured._normalize`` and the other
``_normalize`` helpers): Unicode **NFKC**, collapse every run of whitespace to a
single space, strip, and lowercase. A missing optional field (``author`` or
``date`` is ``None``) normalizes to the empty string, so two reviews with the
same text and date but no author collapse together, while a review that *does*
name an author is distinguished from one that does not. Reviews that differ only
by author, or only by date, keep different keys and are therefore **not** merged.

Keep rule: the **first** occurrence of each key wins and the order of kept
reviews is preserved. Because the per-page extraction stage returns reviews in
page order (page 1 first), the earliest page's copy is the one kept — the lowest
``source_page`` survives a cross-page duplicate. Upload reviews all share
``source_page`` 0, so first-in-list (file order) wins there.

Engineering rules honoured:

* **Stateless** — :func:`dedupe` is a pure function of its input list; it holds
  no state beyond local variables, so a redelivery that rebuilds the same list
  produces the same result.
* **Idempotent** — deduping an already-deduped list is a no-op (design
  Correctness Property 1): the first pass leaves one review per key, so a second
  pass sees no new keys and drops nothing.
* **Review text from elements** — this stage never constructs or edits review
  text; it only compares normalized copies to decide what to drop, and returns
  the original :class:`CollectedReview` objects unchanged.
"""

from __future__ import annotations

import logging
import unicodedata

from app.handlers.extraction_stage import CollectedReview

logger = logging.getLogger(__name__)

#: The dedupe key: normalized (text, author, date). Author/date default to the
#: empty string when the field is ``None`` (missing), so a missing optional
#: field is treated as its own value rather than matching any present value.
DedupeKey = tuple[str, str, str]


def _normalize_field(value: str | None) -> str:
    """Normalize one key field the way the extraction package normalizes text.

    Applies Unicode NFKC (folding compatibility variants such as non-breaking
    spaces and full-width characters), collapses every run of whitespace to a
    single space, strips, and lowercases — the same recipe as
    ``app.extraction.structured._normalize`` and the package's other
    ``_normalize`` helpers, so "same text" means the same thing here as it does
    during extraction. A ``None`` value (a missing optional field) normalizes to
    the empty string.
    """
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    # ``str.split()`` with no argument splits on arbitrary runs of Unicode
    # whitespace and drops empties — the same collapse the cleaner performs.
    return " ".join(normalized.split()).strip().lower()


def _key(collected: CollectedReview) -> DedupeKey:
    """Build the dedupe key for one review: normalized (text, author, date)."""
    review = collected.review
    return (
        _normalize_field(review.text),
        _normalize_field(review.author),
        _normalize_field(review.date),
    )


def dedupe(reviews: list[CollectedReview]) -> list[CollectedReview]:
    """Remove duplicate reviews by normalized text + author + date (Req. 3.3).

    Scans *reviews* in order, keeping the first occurrence of each
    :data:`DedupeKey` and dropping any later review with a key already seen. The
    order of kept reviews is preserved, so with page-ordered input (page 1
    first) the earliest page's copy of a cross-page duplicate is the one kept.

    This is idempotent and complete (design Correctness Property 1): the result
    contains no two reviews with the same normalized text, author, and date, and
    ``dedupe(dedupe(x)) == dedupe(x)`` because a deduped list has one review per
    key and a second pass finds no repeats.

    Args:
        reviews: The combined reviews across all pages (or an upload's reviews),
            each paired with its source page, in page/keep order. Not mutated.

    Returns:
        A new list with duplicates removed, keeping the first occurrence of each
        key and preserving order. The :class:`CollectedReview` objects are
        returned unchanged.
    """
    seen: set[DedupeKey] = set()
    kept: list[CollectedReview] = []
    for collected in reviews:
        key = _key(collected)
        if key in seen:
            continue
        seen.add(key)
        kept.append(collected)

    removed = len(reviews) - len(kept)
    if removed:
        logger.debug("dedupe removed %d duplicate review(s) of %d", removed, len(reviews))
    return kept
