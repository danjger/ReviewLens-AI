"""Per-page extraction for the Extraction Engine (Requirement 6).

``extract_page`` is what the processing Worker calls for every captured page
*after* a plan has been built (``plan.py`` / Task 5).  It dispatches by the
plan's chosen method, reads reviews the cheapest reliable way, falls back to the
Review Locator when a method under-delivers, cross-checks embedded Structured
Review Data, resolves the next page, and returns a full
:class:`~app.extraction.models.PageResult`.

Design: see ``.kiro/specs/review-extraction/design.md`` ("per-page dispatch +
fallback", the ``PageResult`` model, and the Error Handling table).  This module
*composes* the pieces built by earlier tasks — it never re-implements them:

- cleaning: :func:`app.extraction.plan.clean_page` (same path as ``clean``);
- the Locator path: ``clean → locate → postprocess`` (``locator`` + ``postprocess``);
- structured data: :func:`app.extraction.structured.parse_structured`;
- pagination: :func:`app.extraction.pagination.next_page`;
- selector reading reuses ``postprocess`` field helpers (text/rating/date/author
  /title) so a code-read review from a selector is validated **identically** to
  one read from a Locator ref.

Dispatch (Requirement 6.1–6.3), by ``plan.method``:

``selectors``
    Apply the plan's validated selectors to the HTML **with code** (no AI) —
    :func:`extract_by_selectors`.  IF the page yields fewer than *half* the
    reviews-per-page seen on the first page (``plan.first_page.per_page_rate``)
    **and** the caller says it is *not* the last page, fall back to the Review
    Locator and report the fallback (Requirement 6.1).  On the last page a low
    yield is just a low yield — no fallback.  Selectors that *throw* are treated
    as **zero yield** (design Error Handling table), which triggers the same
    fallback when not the last page.

``ai_direct``
    Always read the page with the Review Locator (Requirement 6.2).

``structured``
    Read Structured Review Data; IF the page has none, fall back to the Review
    Locator (Requirement 6.3).

Structured cross-check (Requirement 6.4): WHERE Structured Review Data exists on
a page (under *any* method), structured reviews the chosen method **missed** are
added, and the agreement rate is reported.  ``parse_structured`` already drops
structured reviews whose text is not on the page; those unverifiable ones are
counted under ``discarded["structured_unverified"]`` (we compute the drop count
by comparing the raw structured bodies to the verified ones).

Decisions recorded here (and reported to the orchestrator):

- **method_used** reflects the method *actually used* for the returned reviews.
  When a ``selectors``/``structured`` page falls back to the Locator, the method
  used is ``"ai_direct"`` (the Locator path) and ``fallback=True``.  No fallback
  → ``method_used == plan.method`` and ``fallback=False``.
- **structured_agreement** is reported (non-``None``) exactly when the page has
  Structured Review Data to cross-check against.  It is the share of structured
  reviews that the chosen method also found, by normalized-text containment:
  ``|structured matched by a chosen-method review| / |structured|`` (0.0 when
  structured data exists but nothing overlapped; ``None`` when there is no
  structured data at all).  Deterministic and order-independent.
- **discarded** carries the chosen method's discard counts (postprocess reasons
  for the Locator path; selector-extraction reasons for the selector path) plus
  ``structured_unverified`` when ``parse_structured`` dropped unverifiable
  structured reviews, plus ``structured_added`` recording how many missed
  structured reviews the cross-check added back (informational, not a discard —
  but kept in the same dict so the full picture is in one place).
- **next_page** is resolved by :func:`app.extraction.pagination.next_page`,
  passing the Locator result only when this page was actually read by the
  Locator (so the pagination code can use its next-page ref).
- **blocker / reported_total** come from the Locator result when the Locator
  ran; otherwise ``blocker`` is ``None`` and ``reported_total`` falls back to
  the structured ``review_count``.

Errors: :class:`AIUnavailable` and :class:`LocatorUnavailable` propagate from
the Locator path unchanged (they are retryable; the processing Worker handles
them).  Nothing here swallows them.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser, Node

from app.core.config import get_settings
from app.extraction import cleaner, locator, pagination, postprocess, structured
from app.extraction.models import (
    CleanedPage,
    ExtractionPlan,
    LocatorResult,
    Method,
    NextPage,
    PageResult,
    StructuredResult,
    VerifiedReview,
)
from app.extraction.postprocess import (
    _DEFAULT_RATING_SCALE,
    _MIN_TEXT_LEN,
    _REASON_DUPLICATE,
    _REASON_TOO_SHORT,
    _has_rating_cue,
    _node_text,
    _parse_date,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Discard key: structured reviews ``parse_structured`` dropped because their
#: text did not appear in the page's visible text (design Error Handling table:
#: "Structured data present but unverifiable → ignored, counted in
#: ``discarded['structured_unverified']``").
_REASON_STRUCTURED_UNVERIFIED = "structured_unverified"

#: Informational key: how many missed structured reviews the cross-check added
#: back to the result (Requirement 6.4).  Kept alongside the discard reasons so
#: the full accounting lives in one dict; it is an *addition*, not a discard.
_KEY_STRUCTURED_ADDED = "structured_added"

#: ``method_used`` when a page was read by the Review Locator (either because the
#: plan method is ``ai_direct`` or because a selector/structured page fell back).
_METHOD_AI_DIRECT: Method = "ai_direct"


# ---------------------------------------------------------------------------
# Visible text (shared with the plan builder's approach)
# ---------------------------------------------------------------------------


#: Non-visible subtrees whose text must not count as "visible" when verifying
#: structured reviews — notably ``<script>``, so a JSON-LD review body does not
#: "verify itself" against the page (selectolax ``body.text()`` would otherwise
#: include script text).  Mirrors the cleaner's dropped tags.
_NON_VISIBLE_TAGS: tuple[str, ...] = ("script", "style", "noscript", "template")


def _visible_text(html: str) -> str:
    """Return the page's genuinely visible body text for structured verification.

    ``parse_structured`` checks each structured review's body appears in this
    text (Requirement 3.2); the engine must never treat a JSON-LD/microdata
    review body as present just because it sits in a ``<script>`` block.  So
    this strips the non-visible subtrees (script/style/noscript/template) before
    reading the body text — the same removal the cleaner performs — leaving only
    text a reader would actually see.  An empty/bodyless page yields ``""``.
    """
    tree = HTMLParser(html)
    for tag in _NON_VISIBLE_TAGS:
        for node in tree.css(tag):
            node.decompose()
    body = tree.body
    if body is None:
        return ""
    return body.text()


def _normalize(text: str) -> str:
    """Collapse whitespace and lowercase, matching the rest of the package."""
    return cleaner._WS_RE.sub(" ", text).strip().lower()


# ---------------------------------------------------------------------------
# Selector-based extraction (Requirement 6.1 — code, no AI)
# ---------------------------------------------------------------------------


def _read_field(item: Node, selector: str | None) -> str | None:
    """Read a field's normalized text from an item via a field selector.

    Applies ``selector`` *within* the item element and returns the first match's
    normalized text.  When ``selector`` is ``None`` (the plan has no selector for
    this field) or it matches nothing inside the item, returns ``None`` so the
    caller can fall back (text falls back to the item's own text; other fields
    are simply absent).  Text is always read from the element with code.
    """
    if selector is None:
        return None
    try:
        node = item.css_first(selector)
    except Exception:  # noqa: BLE001 - a bad field selector just means "no field"
        return None
    if node is None:
        return None
    text = _node_text(node)
    return text or None


def _rating_cue_node(item: Node, rating_selector: str | None) -> Node:
    """Return the element to check for a rating cue: the rating field or the item.

    When the plan has a ``rating`` selector and it matches inside the item, that
    element is where the rating cue must live; otherwise the item element itself
    is checked (a card whose wrapper carries ``aria-label="4 out of 5"``).
    """
    if rating_selector is not None:
        try:
            node = item.css_first(rating_selector)
        except Exception:  # noqa: BLE001 - bad selector → fall back to the item
            node = None
        if node is not None:
            return node
    return item


def extract_by_selectors(
    html: str,
    plan: ExtractionPlan,
) -> tuple[list[VerifiedReview], dict[str, int]]:
    """Extract reviews from a page by applying the plan's selectors with code.

    Applies ``plan.selectors.item`` to the original HTML, then within each item
    reads the review text (``selectors.text`` if present, else the item's own
    text), author, date, title, and a rating.  Validation **mirrors**
    :mod:`app.extraction.postprocess` so a selector-read review is kept on the
    same terms as a Locator-read one (steering: review text always comes from
    the element, never invented):

    - text normalized the same way, kept only when ``len >= 15`` (``too_short``);
    - duplicates by normalized text dropped (``duplicate``);
    - a rating kept only when the rating element carries a rating *cue* and
      ``1 <= rating <= rating_scale`` — but selector extraction has no AI value,
      so a numeric rating is parsed from the cue element's text when present;
    - dates parsed with the shared :func:`postprocess._parse_date`.

    Per the design Error Handling table, a selector that **throws** on the page
    is treated as **zero yield**: this returns ``([], {})`` so the caller's
    low-yield fallback fires (when not the last page).

    :param html: Rendered HTML of the page.
    :param plan: The Extraction Plan whose ``selectors`` are applied.
    :returns: ``(reviews, discarded)`` — kept reviews in document order and the
        discard counts by reason.
    """
    item_selector = plan.selectors.item
    if item_selector is None or not item_selector.strip():
        return [], {}

    try:
        tree = HTMLParser(html)
        items = list(tree.css(item_selector))
    except Exception:  # noqa: BLE001 - selectors throw → zero yield (Error Handling)
        return [], {}

    rating_scale = plan.rating_scale if plan.rating_scale is not None else _DEFAULT_RATING_SCALE
    selectors = plan.selectors

    reviews: list[VerifiedReview] = []
    discarded: dict[str, int] = {}
    seen_texts: set[str] = set()

    def discard(reason: str) -> None:
        discarded[reason] = discarded.get(reason, 0) + 1

    for item in items:
        # Text: from the text selector when present, else the item's own text.
        text = _read_field(item, selectors.text)
        if text is None:
            text = _node_text(item) or None
        if text is None or len(text) < _MIN_TEXT_LEN:
            discard(_REASON_TOO_SHORT)
            continue

        dedupe_key = text.lower()
        if dedupe_key in seen_texts:
            discard(_REASON_DUPLICATE)
            continue
        seen_texts.add(dedupe_key)

        author = _read_field(item, selectors.author)
        title = _read_field(item, selectors.title)
        date = _parse_date(_read_field(item, selectors.date))
        rating = _selector_rating(item, selectors.rating, rating_scale)

        reviews.append(
            VerifiedReview(
                text=text,
                rating=rating,
                date=date,
                author=author,
                title=title,
                source_ref=None,  # Read by selector, no cleaner ref.
            )
        )

    return reviews, discarded


def _selector_rating(item: Node, rating_selector: str | None, rating_scale: float) -> float | None:
    """Return a rating read from the item's rating cue, or ``None``.

    Mirrors :func:`postprocess._resolve_rating` but reads the numeric value from
    the cue element's text/attributes with code (there is no AI value in the
    selector path).  A rating is kept only when the element carries a rating cue
    (:func:`postprocess._has_rating_cue`) and the parsed value is in
    ``1 <= value <= rating_scale``.
    """
    node = _rating_cue_node(item, rating_selector)
    if not _has_rating_cue(node):
        return None
    value = _parse_rating_number(node)
    if value is None or not (1 <= value <= rating_scale):
        return None
    return value


def _parse_rating_number(node: Node) -> float | None:
    """Parse a numeric rating from a cue element's attributes or text.

    Looks at the rating-bearing attributes the cleaner keeps (``aria-label``,
    ``title``, ``alt``, ``itemprop``-adjacent ``content``, and any ``*rating*`` /
    ``*score*`` attribute) and finally the element's visible text, taking the
    first number found (e.g. ``"4.5 out of 5 stars"`` → ``4.5``).  Returns
    ``None`` when no number is present.
    """
    number_re = re.compile(r"\d+(?:\.\d+)?")
    attrs = node.attributes

    candidates: list[str] = []
    for name in ("aria-label", "title", "alt", "content"):
        value = attrs.get(name)
        if value:
            candidates.append(value)
    for name, value in attrs.items():
        lowered = name.lower()
        if value and ("rating" in lowered or "score" in lowered):
            candidates.append(value)
    candidates.append(_node_text(node))

    for candidate in candidates:
        match = number_re.search(candidate)
        if match is not None:
            try:
                return float(match.group(0))
            except ValueError:  # pragma: no cover - regex guarantees a number
                continue
    return None


# ---------------------------------------------------------------------------
# Locator path (clean → locate → postprocess) — Requirement 6.2 and fallbacks
# ---------------------------------------------------------------------------


def _locator_path(
    html: str,
    url: str,
    budget_tokens: int,
) -> tuple[list[VerifiedReview], dict[str, int], LocatorResult]:
    """Read a page with the Review Locator: ``clean → locate → postprocess``.

    This is the ``ai_direct`` method and the fallback for the ``selectors`` and
    ``structured`` methods.  Returns the Verified Reviews, the discard counts,
    and the Locator result (so the caller can read its blocker, reported total,
    and next-page ref).

    :raises AIUnavailable: The provider is unavailable or the global AI limit was
        reached (propagated from the Locator).
    :raises LocatorUnavailable: The Locator response failed schema validation
        after the repair retry (propagated from the Locator).
    """
    page: CleanedPage = _clean_page(html, budget_tokens)
    result = locator.locate(page, url=url, title="")
    verified, discarded = postprocess.postprocess(html, page.lookup, result)
    return verified, discarded, result


def _clean_page(html: str, budget_tokens: int) -> CleanedPage:
    """Clean ``html`` into a :class:`CleanedPage` (same path as ``clean``)."""
    result = cleaner.build_clean_result(html)
    tokens = cleaner.count_tokens(result.lines)
    chunks = cleaner.chunk_lines(result.lines, result.lookup, budget_tokens)
    return CleanedPage(lines=result.lines, lookup=result.lookup, chunks=chunks, tokens=tokens)


# ---------------------------------------------------------------------------
# Structured cross-check (Requirement 6.4)
# ---------------------------------------------------------------------------


def _structured_cross_check(
    chosen: list[VerifiedReview],
    structured_result: StructuredResult,
) -> tuple[list[VerifiedReview], float, int]:
    """Add missed structured reviews and compute the agreement rate.

    Called only when the page has verified Structured Review Data.  A structured
    review is "found" by the chosen method when its normalized text is contained
    in (or contains) some chosen-method review's normalized text (the same
    containment match used across the package, since one source may trim
    differently).  Missed structured reviews — those matched by no chosen-method
    review — are appended to the result in structured order.

    Agreement rate (Requirement 6.4): ``|structured found by the chosen method|
    / |structured|``.  With no structured reviews this is not called.

    :returns: ``(combined_reviews, agreement, added_count)``.
    """
    s_norms = [(_normalize(r.text), r) for r in structured_result.reviews]
    chosen_norms = [_normalize(r.text) for r in chosen if _normalize(r.text)]

    def found(structured_norm: str) -> bool:
        if not structured_norm:
            return False
        return any(structured_norm in c or c in structured_norm for c in chosen_norms)

    combined = list(chosen)
    added = 0
    matched = 0
    for norm, review in s_norms:
        if found(norm):
            matched += 1
        else:
            combined.append(review)
            added += 1

    total = len(s_norms)
    agreement = matched / total if total else 0.0
    return combined, agreement, added


def _structured_unverified_count(
    html: str,
    structured_result: StructuredResult,
) -> int:
    """Count structured reviews ``parse_structured`` dropped as unverifiable.

    ``parse_structured`` returns only the structured reviews whose text appears
    in the page; it does not report how many it dropped.  To fill
    ``discarded['structured_unverified']`` (design Error Handling table), this
    re-parses with an **empty** visible text so *no* review verifies, giving the
    total structured-review count, and subtracts the verified count.  The
    difference is the number dropped as unverifiable.

    This is a small extra parse; structured parsing is pure and AI-free, so it
    stays deterministic and cheap.  Returns ``0`` when nothing was dropped.

    To get the *total* number of structured reviews (verifiable or not), it
    re-parses using the script-inclusive raw body text as the verification
    corpus: a JSON-LD review body always appears in its own ``<script>`` text,
    so every structured review verifies and the count is the full total.  The
    difference from the genuinely-verified count is the number dropped.
    """
    raw_body = HTMLParser(html).body
    corpus = raw_body.text() if raw_body is not None else ""
    all_structured = structured.parse_structured(html, corpus)
    dropped = len(all_structured.reviews) - len(structured_result.reviews)
    return max(dropped, 0)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_page(
    html: str,
    url: str,
    plan: ExtractionPlan,
    *,
    is_last: bool,
) -> PageResult:
    """Extract one page according to its Extraction Plan, with fallback.

    See the module docstring for the full dispatch, fallback, cross-check, and
    field semantics.  Short version:

    - dispatch by ``plan.method`` (``selectors`` / ``ai_direct`` / ``structured``);
    - a ``selectors`` page that yields ``< per_page_rate / 2`` reviews (including
      zero, e.g. from selectors that threw) falls back to the Locator **unless**
      ``is_last`` — Requirement 6.1;
    - a ``structured`` page with no structured data falls back to the Locator —
      Requirement 6.3;
    - structured reviews the chosen method missed are added and the agreement
      rate reported whenever the page has Structured Review Data — Requirement
      6.4;
    - the result carries reviews, the method actually used, the fallback flag,
      discard counts, the agreement rate (when it applies), the next-page
      candidate, any blocker, and the reported total — Requirement 6.5.

    :param html: Rendered HTML of the page.
    :param url: The page's final URL.
    :param plan: The Extraction Plan to follow.
    :param is_last: Whether the caller believes this is the last page (suppresses
        the low-yield fallback).
    :returns: The page's :class:`PageResult`.
    :raises AIUnavailable: The provider is unavailable during a Locator read.
    :raises LocatorUnavailable: The Locator could not produce a valid response.
    """
    budget = get_settings().extract_page_token_budget

    # Structured data is read regardless (AI-free) for the cross-check and the
    # structured method.  Verified against the page's visible text.
    structured_result = structured.parse_structured(html, _visible_text(html))

    # --- Dispatch by method, producing the chosen-method reviews. ----------
    reviews: list[VerifiedReview]
    discarded: dict[str, int]
    method_used: Method
    fallback = False
    locator_result: LocatorResult | None = None

    if plan.method == "selectors":
        reviews, discarded = extract_by_selectors(html, plan)
        if _should_fall_back(len(reviews), plan, is_last=is_last):
            reviews, discarded, locator_result = _locator_path(html, url, budget)
            method_used = _METHOD_AI_DIRECT
            fallback = True
        else:
            method_used = "selectors"
    elif plan.method == "structured":
        if structured_result.reviews:
            reviews = list(structured_result.reviews)
            discarded = {}
            method_used = "structured"
        else:
            reviews, discarded, locator_result = _locator_path(html, url, budget)
            method_used = _METHOD_AI_DIRECT
            fallback = True
    else:  # ai_direct
        reviews, discarded, locator_result = _locator_path(html, url, budget)
        method_used = _METHOD_AI_DIRECT

    # --- Structured cross-check (Requirement 6.4). -------------------------
    structured_agreement: float | None = None
    if structured_result.reviews:
        reviews, structured_agreement, added = _structured_cross_check(reviews, structured_result)
        if added:
            discarded = {**discarded, _KEY_STRUCTURED_ADDED: added}

    # Count structured reviews dropped as unverifiable (design Error Handling).
    unverified = _structured_unverified_count(html, structured_result)
    if unverified:
        discarded = {**discarded, _REASON_STRUCTURED_UNVERIFIED: unverified}

    # --- Next page: pass the Locator result only if the Locator ran. -------
    next_candidate: NextPage = pagination.next_page(html, url, plan, locator_result)

    # --- blocker / reported_total from the Locator when it ran. ------------
    blocker = locator_result.blocker if locator_result is not None else None
    reported_total = (
        locator_result.reported_total
        if locator_result is not None and locator_result.reported_total is not None
        else structured_result.review_count
    )

    return PageResult(
        reviews=reviews,
        method_used=method_used,
        fallback=fallback,
        discarded=discarded,
        structured_agreement=structured_agreement,
        next_page=next_candidate,
        blocker=blocker,
        reported_total=reported_total,
    )


def _should_fall_back(yield_count: int, plan: ExtractionPlan, *, is_last: bool) -> bool:
    """Return ``True`` when a ``selectors`` page should fall back to the Locator.

    Requirement 6.1: fall back when the page yields fewer than *half* the
    reviews-per-page seen on the first page (``plan.first_page.per_page_rate``)
    **and** this is not the last page.  A ``per_page_rate`` of 0 (unknown) never
    triggers a fallback — there is no baseline to be below.  On the last page a
    low yield is accepted as-is (no fallback).
    """
    if is_last:
        return False
    per_page_rate = plan.first_page.per_page_rate
    if per_page_rate <= 0:
        return False
    return yield_count < (per_page_rate / 2)
