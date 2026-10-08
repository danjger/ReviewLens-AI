"""Post-processing for the Extraction Engine.

Turns a schema-validated :class:`~app.extraction.models.LocatorResult` into
Verified Reviews.  The Locator only ever points at elements by reference ID;
this module reads every field — text, author, date, title — **from the
referenced page elements with code** (steering: "Review text comes from page
elements via code. AI output may point at elements; it may never supply review
text").  The :class:`LocatorItem` schema has no text field at all, so there is
nothing the AI could supply; this module keeps that invariant explicit by only
reading from resolved elements.

Design: see ``.kiro/specs/review-extraction/design.md`` ("Post-processing
(postprocess.py)").

For each item (Requirement 2.2, 2.3, 2.4):

- Resolve ``item_ref`` and the field refs through the ref lookup with
  :func:`~app.extraction.cleaner.resolve_ref`.  An item whose ``item_ref`` — or
  whose non-null field refs — do not resolve is discarded and counted under
  ``unknown_ref`` (Requirement 2.4).
- Read the review text from ``text_ref`` when present, otherwise from
  ``item_ref`` (a page where the body is the item itself).  Whitespace is
  normalized the same way the cleaner normalizes it (``cleaner._WS_RE``).
- Discard text shorter than 15 characters (``too_short``), text that duplicates
  an already-kept review (``duplicate``), and any item whose ``kind`` is not
  ``review`` (counted by that kind: ``qa``, ``seller_response``,
  ``owner_response``, ``editorial``, ``ad``) (Requirement 2.4).
- Author, date, and title are read the same way from their refs (code, never
  AI).  Dates are parsed with ``dateparser`` and kept as an ISO date when
  unambiguous, otherwise as the raw element text (Requirement 2.2).
- A rating is kept only when the referenced item carries a rating *cue* and the
  value is within ``1 ≤ rating ≤ rating_scale`` (Requirement 2.3 / Property 6).
  A cue is a rating-bearing attribute or class token on the resolved rating
  element (``rating_ref`` when present, otherwise ``item_ref``), matching the
  attributes/classes the cleaner keeps.  Without a cue or out of range, the
  rating is dropped and the review is still kept with ``rating=None``.

Everything here is pure and deterministic: given the same HTML, lookup, and
Locator result, the same reviews and discard counts come out every time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import dateparser

from app.extraction.cleaner import _WS_RE, resolve_ref
from app.extraction.models import LocatorItem, LocatorResult, VerifiedReview

if TYPE_CHECKING:  # pragma: no cover - typing only
    from selectolax.parser import Node

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Minimum length (characters, after whitespace normalization) a review text
#: must reach to be kept (Requirement 2.4).
_MIN_TEXT_LEN = 15

#: Default rating scale assumed when the Locator did not report one.  Five is
#: the near-universal review scale; a cue-bearing rating is still bounded.
_DEFAULT_RATING_SCALE = 5.0

#: Discard reason keys used in the ``discarded`` count dict.  The excluded-kind
#: reasons are the :class:`~app.extraction.models.ItemKind` values other than
#: ``review`` and are counted under their own name.
_REASON_UNKNOWN_REF = "unknown_ref"
_REASON_TOO_SHORT = "too_short"
_REASON_DUPLICATE = "duplicate"

#: Attribute names that always count as a rating cue when present on the rating
#: element — the accessible-label / microdata attributes the cleaner keeps and
#: that carry a rating in practice.
_RATING_CUE_ATTRS: frozenset[str] = frozenset(
    {
        "aria-label",
        "title",
        "alt",
        "itemprop",
    }
)

#: An attribute whose *name* contains one of these substrings is a rating cue
#: (e.g. ``data-rating``, ``data-score``), matching the cleaner's kept-attribute
#: rule.
_RATING_CUE_ATTR_SUBSTRINGS: tuple[str, ...] = ("rating", "score")

#: A class *token* containing one of these substrings is a rating cue (e.g.
#: ``star-5``, ``rating-wrap``), matching the cleaner's kept-class rule.
_RATING_CUE_CLASS_SUBSTRINGS: tuple[str, ...] = ("star", "rating")


# ---------------------------------------------------------------------------
# Element text reading (code, never AI)
# ---------------------------------------------------------------------------


def _node_text(node: Node) -> str:
    """Return a node's visible text, whitespace-normalized.

    Reads the element's text with code (selectolax ``Node.text()``) and collapses
    every run of whitespace to a single space using the cleaner's shared regex,
    so normalization is consistent across the package.

    :param node: The resolved element to read.
    :returns: The normalized text (possibly empty).
    """
    return _WS_RE.sub(" ", node.text()).strip()


def _read_ref_text(html: str, lookup: dict[str, str], ref: str | None) -> str | None:
    """Resolve ``ref`` and return its normalized text, or ``None``.

    Returns ``None`` when ``ref`` is ``None`` (the field was not pointed at) or
    when it does not resolve to an element in the lookup.  Callers distinguish a
    missing optional field (fine) from a required ref that fails to resolve (a
    discard) themselves.
    """
    if ref is None:
        return None
    node = resolve_ref(html, lookup, ref)
    if node is None:
        return None
    text = _node_text(node)
    return text or None


# ---------------------------------------------------------------------------
# Rating cue detection
# ---------------------------------------------------------------------------


def _has_rating_cue(node: Node) -> bool:
    """Return ``True`` when ``node`` carries a rating cue.

    A rating cue is any of (matching the rating-bearing attributes/classes the
    cleaner keeps, Requirement 1.2):

    - a non-empty ``aria-label``, ``title``, ``alt``, or ``itemprop`` attribute;
    - any attribute whose *name* contains ``rating`` or ``score``;
    - a ``class`` token containing ``star`` or ``rating``.

    This is what lets an AI-interpreted rating be trusted: the value is only kept
    when the element it points at actually bears a rating signal (Requirement
    2.3), never on the AI's say-so alone.
    """
    attrs = node.attributes

    for name in _RATING_CUE_ATTRS:
        value = attrs.get(name)
        if value is not None and value.strip():
            return True

    for name in attrs:
        lowered = name.lower()
        if any(sub in lowered for sub in _RATING_CUE_ATTR_SUBSTRINGS):
            return True

    class_value = attrs.get("class")
    if class_value:
        for token in class_value.split():
            if any(sub in token.lower() for sub in _RATING_CUE_CLASS_SUBSTRINGS):
                return True

    return False


def _resolve_rating(
    html: str,
    lookup: dict[str, str],
    item: LocatorItem,
    rating_scale: float,
) -> float | None:
    """Return the rating to keep for an item, or ``None`` to drop it.

    A rating is kept only when (Requirement 2.3 / Property 6):

    1. the AI supplied a ``rating_value``;
    2. the referenced rating element — ``rating_ref`` when the AI pointed at one,
       otherwise the item element (``item_ref``) — carries a rating cue; and
    3. the value is in range: ``1 ≤ rating_value ≤ rating_scale``.

    When any condition fails, the rating is dropped (``None``) and the review is
    still kept — a review without a trustworthy rating is a valid review.
    """
    value = item.rating_value
    if value is None:
        return None

    if not (1 <= value <= rating_scale):
        return None

    cue_ref = item.rating_ref if item.rating_ref is not None else item.item_ref
    node = resolve_ref(html, lookup, cue_ref)
    if node is None or not _has_rating_cue(node):
        return None

    return value


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


def _parse_date(raw: str | None) -> str | None:
    """Parse a review date, keeping an ISO date when unambiguous.

    Uses ``dateparser`` with ``PREFER_DAY_OF_MONTH="first"`` so a parseable date
    is normalized to an ISO ``YYYY-MM-DD`` string.  When ``dateparser`` cannot
    make sense of the text (garbage, relative phrases it rejects, or no date at
    all), the raw element text is kept instead, so nothing is lost and the
    caller can still display what the page showed (Requirement 2.2).

    :param raw: The normalized text read from the date element, or ``None``.
    :returns: An ISO date string when parseable, else the raw text, else
        ``None`` when there was no date text at all.
    """
    if raw is None:
        return None
    parsed = dateparser.parse(raw)
    if parsed is None:
        return raw  # Unparseable: keep the raw text (Requirement 2.2).
    return str(parsed.date().isoformat())


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def postprocess(
    html: str,
    lookup: dict[str, str],
    result: LocatorResult,
) -> tuple[list[VerifiedReview], dict[str, int]]:
    """Turn a Locator result into Verified Reviews plus discard counts.

    Reads every field from the referenced page elements with code and applies
    the keep/discard rules in Requirement 2.2–2.4.  The returned discard dict is
    keyed by reason (``unknown_ref``, ``too_short``, ``duplicate``, and each
    excluded ``kind``) with the number of items dropped for that reason, matching
    how :attr:`~app.extraction.models.PageResult.discarded` is used.  Reasons
    with a zero count are omitted.

    Order of checks per item (first failure wins, so each discard is counted
    once):

    1. ``kind != "review"`` → counted by that kind.
    2. ``item_ref`` does not resolve → ``unknown_ref``.
    3. a non-null field ref (``text_ref``/``author_ref``/``date_ref``/
       ``title_ref``/``rating_ref``) does not resolve → ``unknown_ref``.
    4. text (from ``text_ref``, else ``item_ref``) shorter than 15 chars →
       ``too_short``.
    5. text duplicates an already-kept review → ``duplicate``.

    A surviving item becomes a :class:`VerifiedReview` whose ``text`` came from
    the page, with the rating applied only when cue-backed and in range, the date
    parsed to ISO when unambiguous, and ``source_ref`` set to ``text_ref`` when
    present (the element the text was read from) else ``item_ref``, for
    traceability.

    :param html: The original rendered HTML the Cleaned Page was built from.
    :param lookup: The ref-to-element lookup from the Cleaned Page.
    :param result: The schema-validated Locator result to post-process.
    :returns: ``(reviews, discarded)`` — the kept reviews in Locator order and
        the discard counts by reason.
    """
    rating_scale = result.rating_scale if result.rating_scale is not None else _DEFAULT_RATING_SCALE

    reviews: list[VerifiedReview] = []
    discarded: dict[str, int] = {}
    seen_texts: set[str] = set()

    def discard(reason: str) -> None:
        discarded[reason] = discarded.get(reason, 0) + 1

    for item in result.items:
        # 1. Only customer reviews survive; everything else is counted by kind.
        if item.kind != "review":
            discard(item.kind)
            continue

        # 2. The item element itself must resolve.
        if resolve_ref(html, lookup, item.item_ref) is None:
            discard(_REASON_UNKNOWN_REF)
            continue

        # 3. Any field ref the AI supplied must resolve, or the item is suspect.
        field_refs = (
            item.text_ref,
            item.author_ref,
            item.date_ref,
            item.title_ref,
            item.rating_ref,
        )
        if any(ref is not None and resolve_ref(html, lookup, ref) is None for ref in field_refs):
            discard(_REASON_UNKNOWN_REF)
            continue

        # 4. Read text with code: from text_ref when given, else the item itself.
        text_source_ref = item.text_ref if item.text_ref is not None else item.item_ref
        text = _read_ref_text(html, lookup, text_source_ref)
        if text is None or len(text) < _MIN_TEXT_LEN:
            discard(_REASON_TOO_SHORT)
            continue

        # 5. Drop duplicates by normalized text (whitespace already collapsed).
        dedupe_key = text.lower()
        if dedupe_key in seen_texts:
            discard(_REASON_DUPLICATE)
            continue
        seen_texts.add(dedupe_key)

        # Surviving review: read remaining fields by code; apply rating/date rules.
        author = _read_ref_text(html, lookup, item.author_ref)
        title = _read_ref_text(html, lookup, item.title_ref)
        date = _parse_date(_read_ref_text(html, lookup, item.date_ref))
        rating = _resolve_rating(html, lookup, item, rating_scale)

        reviews.append(
            VerifiedReview(
                text=text,
                rating=rating,
                date=date,
                author=author,
                title=title,
                source_ref=text_source_ref,
            )
        )

    return reviews, discarded
