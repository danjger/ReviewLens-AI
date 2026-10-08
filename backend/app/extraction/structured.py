"""Structured review data parsing for the Extraction Engine.

Reads embedded JSON-LD and microdata ``Review`` and ``AggregateRating`` objects
without any AI call, so the result can cross-check the Review Locator and serve
as a fallback (Requirement 3).

Design: see ``.kiro/specs/review-extraction/design.md`` ("Structured data
(structured.py)").

Both syntaxes are extracted with ``extruct`` using ``uniform=True``, which maps
microdata into the same schema.org shape JSON-LD already uses (``@type`` plus
property keys), so a single walker handles both.  ``Review`` objects are found
at any depth under the supported container types — ``Product``,
``Organization``, ``LocalBusiness``, and ``SoftwareApplication`` (Requirement
3.1) — as well as reviews that appear at the top level.

A structured review is kept only if its normalized ``reviewBody`` appears in the
page's normalized visible text (Requirement 3.2 / Property 1): the engine never
surfaces review text that isn't actually on the page.  Normalization is Unicode
NFKC plus whitespace collapse, matching the whitespace approach the cleaner uses
(``cleaner._WS_RE``), so verification is consistent across the package.

Output is deterministic: objects are walked in document order and reviews are
emitted in the order they are encountered.
"""

from __future__ import annotations

import unicodedata
from typing import Any

import extruct

from app.extraction.models import StructuredResult, VerifiedReview

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Container schema.org types under which nested ``Review`` objects are read
#: (Requirement 3.1).  Compared case-insensitively against the trailing type
#: name, so both ``Product`` and ``https://schema.org/Product`` match.
_CONTAINER_TYPES: frozenset[str] = frozenset(
    {
        "product",
        "organization",
        "localbusiness",
        "softwareapplication",
    }
)

#: The schema.org type name of a review object.
_REVIEW_TYPE = "review"

#: Keys that may hold nested review objects on a container.
_REVIEW_KEYS: tuple[str, ...] = ("review", "reviews")

#: Keys that may hold an aggregate-rating object on a container.
_AGGREGATE_KEYS: tuple[str, ...] = ("aggregateRating", "aggregaterating")

#: Source reference recorded on every structured review for traceability.
_SOURCE_REF = "structured"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Normalize text for the visible-text verification check.

    Applies Unicode NFKC normalization (folding compatibility variants such as
    non-breaking spaces and full-width characters) and collapses every run of
    whitespace to a single space, then strips and lowercases.  Lowercasing makes
    verification robust to trivial case differences between structured data and
    rendered text while still requiring the words themselves to match.

    :param text: Raw text from structured data or the page.
    :returns: The normalized form used on both sides of the containment check.
    """
    normalized = unicodedata.normalize("NFKC", text)
    # ``str.split()`` with no argument splits on arbitrary runs of Unicode
    # whitespace and drops empties — the same collapse the cleaner performs.
    return " ".join(normalized.split()).strip().lower()


# ---------------------------------------------------------------------------
# Schema.org helpers
# ---------------------------------------------------------------------------


def _type_names(obj: dict[str, Any]) -> list[str]:
    """Return the lower-cased trailing type names of a schema.org object.

    ``@type`` may be a string or a list of strings, and each value may be a bare
    name (``Product``) or a full IRI (``https://schema.org/Product``).  The
    trailing path/fragment segment is taken and lower-cased so comparisons are
    uniform.
    """
    raw = obj.get("@type")
    values: list[str]
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [v for v in raw if isinstance(v, str)]
    else:
        values = []
    names: list[str] = []
    for value in values:
        tail = value.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        names.append(tail.strip().lower())
    return names


def _is_type(obj: object, type_name: str) -> bool:
    """Return ``True`` when ``obj`` is a dict whose type set includes ``type_name``."""
    return isinstance(obj, dict) and type_name in _type_names(obj)


def _as_list(value: object) -> list[object]:
    """Return ``value`` as a list: wrap a single object, pass a list through."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _text_value(value: object) -> str | None:
    """Extract a plain string from a schema.org value.

    Values may be a bare string or a nested object (for example ``author`` as a
    ``Person`` with a ``name``).  For objects, the ``name`` property is used.
    Returns ``None`` when no usable string is present.
    """
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    if isinstance(value, list):
        for item in value:
            text = _text_value(item)
            if text is not None:
                return text
    return None


def _rating_value(review: dict[str, Any]) -> float | None:
    """Read a numeric rating from a review's ``reviewRating``.

    ``reviewRating`` is typically a ``Rating`` object carrying ``ratingValue``;
    some pages use a bare number or string.  Returns the value as a float, or
    ``None`` when absent or unparseable.
    """
    rating = review.get("reviewRating")
    candidate: Any = None
    if isinstance(rating, dict):
        candidate = rating.get("ratingValue")
    elif isinstance(rating, (str, int, float)):
        candidate = rating
    if candidate is None:
        return None
    try:
        return float(str(candidate).strip())
    except (TypeError, ValueError):
        return None


def _review_count(aggregate: object) -> int | None:
    """Read the review count from an ``AggregateRating`` object.

    Prefers ``reviewCount``; falls back to ``ratingCount`` when ``reviewCount``
    is absent (Requirement 3.1 reads review counts, preferring ``reviewCount``).
    Returns ``None`` when neither is present or parseable.
    """
    if not isinstance(aggregate, dict):
        return None
    for key in ("reviewCount", "reviewcount", "ratingCount", "ratingcount"):
        value = aggregate.get(key)
        if value is None:
            continue
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Review mapping
# ---------------------------------------------------------------------------


def _review_body(review: dict[str, Any]) -> str | None:
    """Return the review's body text (``reviewBody``, then ``description``)."""
    for key in ("reviewBody", "reviewbody", "description"):
        text = _text_value(review.get(key))
        if text is not None:
            return text
    return None


def _map_review(review: dict[str, Any]) -> VerifiedReview | None:
    """Map a schema.org ``Review`` object to a :class:`VerifiedReview`.

    Returns ``None`` when the review carries no body text (nothing to verify or
    surface).  Rating, date, author, and title are mapped when present.
    """
    body = _review_body(review)
    if body is None:
        return None
    author = _text_value(review.get("author"))
    date = _text_value(review.get("datePublished")) or _text_value(review.get("datepublished"))
    title = _text_value(review.get("name")) or _text_value(review.get("headline"))
    return VerifiedReview(
        text=body,
        rating=_rating_value(review),
        date=date,
        author=author,
        title=title,
        source_ref=_SOURCE_REF,
    )


# ---------------------------------------------------------------------------
# Tree walking
# ---------------------------------------------------------------------------


def _iter_objects(data: object) -> list[dict[str, Any]]:
    """Return every schema.org object found anywhere in ``data``, in order.

    Walks dicts and lists recursively.  ``@graph`` lists and nested property
    values are all traversed, so container and review objects are found wherever
    they sit in the structure.
    """
    found: list[dict[str, Any]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def _collect_reviews(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect ``Review`` objects at any depth under a container object.

    Looks under the ``review``/``reviews`` properties and also scans the whole
    subtree, so reviews nested deeper (for example under an intermediate list or
    wrapper) are still found (Requirement 3.1: "at any depth").  De-duplicates by
    object identity to keep output deterministic without repeats.
    """
    reviews: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(candidate: dict[str, Any]) -> None:
        if _is_type(candidate, _REVIEW_TYPE) and id(candidate) not in seen:
            seen.add(id(candidate))
            reviews.append(candidate)

    for key in _REVIEW_KEYS:
        for candidate in _as_list(obj.get(key)):
            for nested in _iter_objects(candidate):
                add(nested)

    # Also scan the container's full subtree for reviews placed outside the
    # ``review`` property (some pages nest them under other properties).
    for nested in _iter_objects(obj):
        if nested is obj:
            continue
        add(nested)

    return reviews


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_structured(html: str, visible_text: str) -> StructuredResult:
    """Read JSON-LD and microdata review data from a page.

    Extracts both syntaxes with ``extruct`` (``uniform=True`` so microdata and
    JSON-LD share one schema.org shape), finds every ``Review`` nested under the
    supported container types and at the top level, and reads the first
    ``AggregateRating`` review count encountered.  Each review is kept only if
    its normalized ``reviewBody`` appears in the normalized ``visible_text``
    (Requirement 3.2); reviews that fail this check are dropped (callers count
    them as ``structured_unverified`` later).

    Malformed structured data is tolerated: ``extruct`` parses leniently and a
    page with no structured data yields an empty result.

    :param html: Rendered HTML of the page.
    :param visible_text: The page's visible text, used to verify each review.
    :returns: The verified structured reviews and the reported review count.
    """
    try:
        data = extruct.extract(
            html,
            syntaxes=["json-ld", "microdata"],
            uniform=True,
        )
    except Exception:  # noqa: BLE001 - extruct can raise on pathological markup
        return StructuredResult()

    objects: list[dict[str, Any]] = []
    for syntax in ("json-ld", "microdata"):
        objects.extend(_iter_objects(data.get(syntax, [])))

    normalized_visible = _normalize(visible_text)

    reviews: list[VerifiedReview] = []
    seen_review_ids: set[int] = set()
    seen_texts: set[str] = set()
    review_count: int | None = None

    for obj in objects:
        type_names = _type_names(obj)

        # Record the first aggregate review count we encounter, from a container
        # object's ``aggregateRating`` or a standalone ``AggregateRating``.
        if review_count is None:
            if any(name in _CONTAINER_TYPES for name in type_names):
                for key in _AGGREGATE_KEYS:
                    count = _review_count(obj.get(key))
                    if count is not None:
                        review_count = count
                        break
            if review_count is None and "aggregaterating" in type_names:
                review_count = _review_count(obj)

        # Collect reviews nested under containers, and top-level reviews.
        candidate_reviews: list[dict[str, Any]] = []
        if any(name in _CONTAINER_TYPES for name in type_names):
            candidate_reviews.extend(_collect_reviews(obj))
        elif _REVIEW_TYPE in type_names:
            candidate_reviews.append(obj)

        for review_obj in candidate_reviews:
            if id(review_obj) in seen_review_ids:
                continue
            seen_review_ids.add(id(review_obj))
            mapped = _map_review(review_obj)
            if mapped is None:
                continue
            normalized_body = _normalize(mapped.text)
            if not normalized_body or normalized_body not in normalized_visible:
                continue  # Unverifiable: dropped (Requirement 3.2).
            if normalized_body in seen_texts:
                continue  # Deterministic de-duplication of identical bodies.
            seen_texts.add(normalized_body)
            reviews.append(mapped)

    return StructuredResult(reviews=reviews, review_count=review_count)
