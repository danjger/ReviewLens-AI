"""Extraction Plan builder for the Extraction Engine.

``build_plan`` is what a URL Check calls on a first page.  It composes the
pieces built by earlier tasks — the cleaner, structured-data parser, Review
Locator, post-processing, and selector validation — into an
:class:`~app.extraction.models.ExtractionPlan` plus the first page's
:class:`~app.extraction.models.PageResult` (Requirement 4).

Pipeline (mirrors the design flowchart ``clean → locate → postprocess →
selectors + structured → plan``):

1. **Clean** the page with :func:`app.extraction.clean` (deterministic).
2. **Structured data** is read regardless with :func:`app.extraction.parse_structured`
   — it is AI-free, cross-checks the Locator, and is the degraded fallback.
3. **Locate** reviews with :func:`app.extraction.locate` (the one AI call).
   - :class:`AIUnavailable` → build the **degraded** structured-only plan
     (Requirement 4.4) so callers can degrade gracefully.
   - :class:`LocatorUnavailable` is *not* caught here: per the design's Error
     Handling table it is a hard retryable error, so it propagates.
4. **Post-process** the Locator result into Verified Reviews + discard counts.
5. **Validate** the suggested selectors against the Verified Reviews.
6. **Choose the method** per Requirement 4.2:
   - selectors valid → ``selectors``;
   - else structured holds *more* verified reviews than the Locator found *and*
     their texts agree → ``structured``;
   - else → ``ai_direct``.
7. **Derive the next-page rule** (Requirement 4.3 plan field) from the Locator's
   next-page ref, converted to a stable CSS selector; else ``type="none"``.
   Full generic/URL-template pagination resolution is Task 6 (``pagination.py``).
8. **Assemble** the :class:`ExtractionPlan` with every field in the design JSON
   and the first-page :class:`PageResult`.

Design decisions recorded here (also reported back to the orchestrator):

- **Visible text source** for structured verification: the raw page body text
  read with selectolax (``HTMLParser(html).body.text()``).  This is the full
  rendered visible text, which is exactly what ``parse_structured`` normalizes
  and runs its containment check against; it does not depend on the cleaner's
  line truncation.
- **"Texts agree"**: a deterministic overlap check between the structured
  reviews' normalized texts and the Locator's verified normalized texts — see
  :func:`_texts_agree`.  Two reviews "match" by normalized containment (either
  contains the other), consistent with the rest of the package; the sets agree
  when at least :data:`_AGREEMENT_OVERLAP` of the smaller set has a match.
- **Next-page rule scope**: here we only derive a CSS-selector rule from the
  Locator's next-page element (or ``none``).  Generic patterns and URL-template
  inference are Task 6.
- **created_at**: the only non-deterministic field; injectable via ``now`` so
  tests can pin it (defaults to :func:`datetime.datetime.now` in UTC).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from selectolax.parser import HTMLParser

from app.core.ai import resolve_model
from app.core.config import get_settings
from app.extraction import cleaner, locator, postprocess, selectors, structured
from app.extraction.errors import AIUnavailable
from app.extraction.extract import extract_by_selectors
from app.extraction.models import (
    CleanedPage,
    ExtractionPlan,
    FirstPageStats,
    LocatorResult,
    LocatorSelectors,
    Method,
    NextPage,
    NextPageRule,
    PageResult,
    StructuredResult,
    VerifiedReview,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: AI purpose for the extract model, so the plan records the configured model ID
#: (never a literal) and resolution matches what the Locator actually uses.
_EXTRACT_PURPOSE = "extract_locator"  # noqa: S105 - AI purpose label, not a secret

#: Minimum share of the smaller review set that must have a containment match in
#: the other set for the structured and Locator texts to be considered to
#: "agree" (Requirement 4.2).  A modest, deterministic threshold: a clear
#: majority overlap, not an exact one, since structured bodies and the verified
#: field text are read from different places and may differ in trimming.
_AGREEMENT_OVERLAP = 0.5

#: Rule name recorded on the first-page ``NextPage`` so callers can see which
#: plan field produced it.  Full resolution to a URL is Task 6 (pagination).
_NEXT_RULE_FROM_PLAN = "plan_rule"
_NEXT_RULE_NONE = "none"


# ---------------------------------------------------------------------------
# Visible text
# ---------------------------------------------------------------------------


def _visible_text(html: str) -> str:
    """Return the page's visible text for structured-data verification.

    Reads the body text with selectolax.  ``parse_structured`` normalizes this
    and checks that each structured review's body appears in it, so the full
    rendered text (not the cleaner's truncated line snippets) is the correct,
    simplest source.  An empty/bodyless page yields ``""``.
    """
    body = HTMLParser(html).body
    if body is None:
        return ""
    return body.text()


# ---------------------------------------------------------------------------
# "Texts agree" between structured and Locator reviews
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Collapse whitespace the same way the rest of the package does."""
    return cleaner._WS_RE.sub(" ", text).strip().lower()


def _texts_agree(
    structured_reviews: list[VerifiedReview],
    verified_reviews: list[VerifiedReview],
) -> bool:
    """Return ``True`` when the two review sets' texts overlap enough to agree.

    Used by the method choice (Requirement 4.2): ``structured`` is only chosen
    when Structured Review Data holds *more* verified reviews than the Locator
    found **and** their texts agree — i.e. the structured reviews are plausibly
    the same reviews, just read more completely, not a different set.

    Definition (deterministic, order-independent): normalize both sides, then a
    verified review "matches" a structured review when either normalized text
    contains the other (consistent with selector validation's containment
    matching, since one source may trim differently).  The sets agree when at
    least :data:`_AGREEMENT_OVERLAP` of the *smaller* non-empty set has a match
    in the other.  Two empty sets do not agree (nothing to compare).
    """
    s_texts = [t for t in (_normalize(r.text) for r in structured_reviews) if t]
    v_texts = [t for t in (_normalize(r.text) for r in verified_reviews) if t]
    if not s_texts or not v_texts:
        return False

    def _contained(a: str, b: str) -> bool:
        return a in b or b in a

    matched_v = sum(1 for v in v_texts if any(_contained(v, s) for s in s_texts))
    matched_s = sum(1 for s in s_texts if any(_contained(s, v) for v in v_texts))

    smaller = min(len(s_texts), len(v_texts))
    # Share of the smaller set that has a counterpart in the other set.
    overlap = min(matched_v, matched_s) / smaller
    return overlap >= _AGREEMENT_OVERLAP


# ---------------------------------------------------------------------------
# Under-listed-selector recovery
# ---------------------------------------------------------------------------


def _selector_covers_verified(
    recovered: list[VerifiedReview],
    verified: list[VerifiedReview],
) -> bool:
    """True when every verified review's text is reproduced by a recovered one.

    Normalized-containment match (consistent with selector validation): a
    verified review is covered when its normalized text is contained in some
    recovered review's normalized text (or vice versa). Used to confirm the
    selector's fuller extraction is a SUPERSET of what the Locator verified,
    so recovery only adds missed reviews and never swaps in a different set.
    """
    rec = [t for t in (_normalize(r.text) for r in recovered) if t]
    ver = [t for t in (_normalize(r.text) for r in verified) if t]
    if not ver:
        return True
    return all(any(v in r or r in v for r in rec) for v in ver)


def _recover_underlisted_selectors(
    html: str,
    locator_result: LocatorResult,
    verified: list[VerifiedReview],
    validation: selectors.SelectorValidation,
) -> list[VerifiedReview] | None:
    """Re-verify from the selector's items when the Locator under-listed items.

    A sound narrowing of the over-selection rule. When the Locator suggests a
    correct ``item`` selector but lists only a few of the reviews it actually
    matches (observed with some models), selector validation rejects it purely
    on over-selection (the many unlisted-but-real cards look like noise), and
    the method falls to ``ai_direct`` with only the under-listed items — a
    recall collapse.

    This recovers that case **without** weakening Property 8 (selector
    soundness). It triggers only when:

    * the selector reproduced **every** verified review (``agreement == 1.0``),
      so the selector is proven correct on everything we did verify; and
    * it was rejected (``not is_valid``) — i.e. only over-selection, since
      agreement is already perfect.

    It then re-reads reviews from the selector's own items with
    :func:`app.extraction.extract.extract_by_selectors` — the exact code the
    ``selectors`` method uses on later pages, which applies the same
    post-processing filters (short text, bad rating, dedupe), so re-verified
    reviews are held to the same bar and genuine noise is still dropped. The
    fuller set is adopted only when it **strictly exceeds** what the Locator
    listed; otherwise the recovery is a no-op (returns ``None``).
    """
    if validation.is_valid:
        return None
    item_selector = locator_result.selectors.item
    if item_selector is None or not item_selector.strip():
        return None

    # Re-read reviews from the selector's own items with the SAME code + filters
    # the ``selectors`` method uses on later pages (short text, bad rating,
    # dedupe), so recovered reviews meet the same bar and genuine noise is
    # dropped.
    probe_plan = ExtractionPlan(
        version=1,
        created_at="",
        locator_model=None,
        prompt_version=locator.PROMPT_VERSION,
        method="selectors",
        selectors=locator_result.selectors,
        rating_scale=locator_result.rating_scale,
        next_page_rule=NextPageRule(type="none"),
        first_page=FirstPageStats(verified=0, discarded=0, structured_count=0, per_page_rate=0),
        reported_total=None,
        entity_hint=None,
        confidence=None,
        degraded=False,
    )
    recovered, _ = extract_by_selectors(html, probe_plan)

    # Only recover when the selector genuinely found MORE than the Locator
    # listed (the under-listing signature). If it found no more, there is
    # nothing to gain and we must not override the normal choice.
    if len(recovered) <= len(verified):
        return None

    # Soundness (Property 8): the selector must reproduce EVERY review the
    # Locator did verify — i.e. the extra items are additional reviews the
    # Locator missed, not a different (noisier) set. With an empty verified set
    # (the Locator under-listed so hard nothing survived post-processing) there
    # is nothing to contradict, and the recovered set already passed the shared
    # review filters, so the uniform selector extraction is trusted.
    if verified and not _selector_covers_verified(recovered, verified):
        return None

    return recovered


# ---------------------------------------------------------------------------
# Method choice (Requirement 4.2)
# ---------------------------------------------------------------------------


def _choose_method(
    selector_valid: bool,
    structured_count: int,
    locator_found: int,
    texts_agree: bool,
) -> Method:
    """Choose the extraction method per Requirement 4.2.

    - ``selectors`` when the suggested selectors validated;
    - else ``structured`` when Structured Review Data holds *more* verified
      reviews than the Locator found *and* their texts agree;
    - else ``ai_direct``.
    """
    if selector_valid:
        return "selectors"
    if structured_count > locator_found and texts_agree:
        return "structured"
    return "ai_direct"


# ---------------------------------------------------------------------------
# Next-page rule derivation (Requirement 4.3 plan field)
# ---------------------------------------------------------------------------


def _next_page_rule(html: str, lookup: dict[str, str], result: LocatorResult) -> NextPageRule:
    """Derive the plan's next-page rule from the Locator's next-page element.

    Converts the Locator's ``next_page.ref`` into a reusable CSS selector by
    resolving it through the cleaner lookup to the element's stable CSS path in
    the original DOM.  When there is no next-page ref, or it does not resolve,
    the rule is ``type="none"``.

    Scope: this derives only a *selector* rule (or none).  Generic pagination
    patterns and URL-template inference (Requirement 5) are implemented in Task
    6 (``pagination.py``); the plan simply records a stable selector when the
    Locator pointed at a next-page control.
    """
    ref = result.next_page.ref
    if ref is None:
        return NextPageRule(type="none")
    css = lookup.get(ref)
    if css is None or cleaner.resolve_ref(html, lookup, ref) is None:
        return NextPageRule(type="none")
    return NextPageRule(type="selector", css=css)


# ---------------------------------------------------------------------------
# Degraded (structured-only) plan — AI unavailable (Requirement 4.4)
# ---------------------------------------------------------------------------


def _degraded_plan(
    structured_result: StructuredResult,
    *,
    now: datetime,
) -> tuple[ExtractionPlan, PageResult]:
    """Build a structured-only plan when the AI is unavailable (Requirement 4.4).

    With no Locator result, the plan is built from Structured Review Data alone,
    marked ``degraded=True`` and ``method="structured"`` so callers know to
    degrade gracefully: no selectors, unknown rating scale, and a next-page rule
    of ``none`` (generic pagination can still be tried at extract time, but the
    plan derives nothing without a Locator).  The provenance fields still record
    the configured locator model and the prompt version for traceability.
    """
    reviews = list(structured_result.reviews)
    reported_total = structured_result.review_count
    plan = ExtractionPlan(
        version=1,
        created_at=now.isoformat(),
        locator_model=_safe_model(),
        prompt_version=locator.PROMPT_VERSION,
        method="structured",
        selectors=LocatorSelectors(),
        rating_scale=None,
        next_page_rule=NextPageRule(type="none"),
        first_page=FirstPageStats(
            verified=len(reviews),
            discarded=0,
            structured_count=len(reviews),
            per_page_rate=len(reviews),
        ),
        reported_total=reported_total,
        entity_hint=None,
        confidence=None,
        degraded=True,
    )
    page = PageResult(
        reviews=reviews,
        method_used="structured",
        fallback=False,
        discarded={},
        structured_agreement=None,
        next_page=NextPage(url=None, rule_used=_NEXT_RULE_NONE, reason_if_none="ai_unavailable"),
        blocker=None,
        reported_total=reported_total,
    )
    return plan, page


def _safe_model() -> str | None:
    """Return the configured extract model ID, or ``None`` if unresolvable.

    Model IDs always come from config (never literals).  Wrapped so a degraded
    plan can still be built even if settings cannot resolve the model for some
    reason — provenance is best-effort when the AI path failed.
    """
    try:
        return resolve_model(_EXTRACT_PURPOSE)
    except Exception:  # noqa: BLE001 - provenance is best-effort on the degraded path
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_plan(
    html: str,
    url: str,
    title: str,
    *,
    now: Callable[[], datetime] | None = None,
) -> tuple[ExtractionPlan, PageResult]:
    """Build an Extraction Plan and first-page result for a first page.

    See the module docstring for the pipeline and the recorded design decisions.

    :param html: Rendered HTML of the first page.
    :param url: The page's final URL (recorded context; pagination is Task 6).
    :param title: The page's title (Locator prompt context).
    :param now: Optional clock returning the ``created_at`` timestamp; defaults
        to :func:`datetime.datetime.now` in UTC.  Injectable so tests can pin
        the only non-deterministic field.
    :returns: ``(plan, first_page_result)``.
    :raises LocatorUnavailable: The Locator could not produce a valid response
        (hard retryable error; propagated per the design).
    """
    clock = now if now is not None else (lambda: datetime.now(UTC))
    created_at = clock()

    settings = get_settings()
    budget = settings.extract_page_token_budget

    # 1. Clean the page (deterministic, AI-free).
    page: CleanedPage = clean_page(html, budget)

    # 2. Structured data is read regardless — AI-free cross-check and fallback.
    structured_result = structured.parse_structured(html, _visible_text(html))

    # 3. Locate reviews (the AI call).  AIUnavailable → degraded plan (Req 4.4).
    #    LocatorUnavailable is a hard retryable error and is left to propagate.
    try:
        locator_result = locator.locate(page, url=url, title=title)
    except AIUnavailable:
        return _degraded_plan(structured_result, now=created_at)

    # 4. Post-process into Verified Reviews + discard counts (code reads text).
    verified, discarded = postprocess.postprocess(html, page.lookup, locator_result)

    # 5. Validate the suggested selectors against the Verified Reviews.
    validation = selectors.validate_selectors(
        html,
        locator_result.selectors,
        verified,
        min_agreement=settings.selector_min_agreement,
    )

    # 5b. Recover under-listed selectors: if the selector reproduces every
    #     verified review but was rejected on over-selection (the Locator
    #     listed only some of the reviews its own selector matches), re-verify
    #     from the selector's items so `auto` keeps the fuller set instead of
    #     collapsing to the under-listed `ai_direct` result.
    recovered = _recover_underlisted_selectors(html, locator_result, verified, validation)
    selector_valid = validation.is_valid
    if recovered is not None:
        verified = recovered
        discarded = {}
        selector_valid = True

    # 6. Choose the method (Requirement 4.2).
    structured_count = len(structured_result.reviews)
    locator_found = len(verified)
    texts_agree = _texts_agree(structured_result.reviews, verified)
    method = _choose_method(
        selector_valid,
        structured_count,
        locator_found,
        texts_agree,
    )

    # 7. Derive the next-page rule (selector or none; full pagination is Task 6).
    next_rule = _next_page_rule(html, page.lookup, locator_result)

    # 8. Assemble the plan and the first-page result.
    plan_selectors = locator_result.selectors if method == "selectors" else LocatorSelectors()
    discarded_total = sum(discarded.values())
    reported_total = (
        locator_result.reported_total
        if locator_result.reported_total is not None
        else structured_result.review_count
    )

    plan = ExtractionPlan(
        version=1,
        created_at=created_at.isoformat(),
        locator_model=_safe_model(),
        prompt_version=locator.PROMPT_VERSION,
        method=method,
        selectors=plan_selectors,
        rating_scale=locator_result.rating_scale,
        next_page_rule=next_rule,
        first_page=FirstPageStats(
            verified=locator_found,
            discarded=discarded_total,
            structured_count=structured_count,
            per_page_rate=locator_found,
        ),
        reported_total=reported_total,
        entity_hint=locator_result.entity_hint,
        confidence=locator_result.confidence,
        degraded=False,
    )

    rule_used = _NEXT_RULE_FROM_PLAN if next_rule.type != "none" else _NEXT_RULE_NONE
    reason_if_none = None if next_rule.type != "none" else "no_next_page_element"
    first_page_result = PageResult(
        reviews=verified,
        method_used=method,
        fallback=False,
        discarded=discarded,
        structured_agreement=None,
        next_page=NextPage(url=None, rule_used=rule_used, reason_if_none=reason_if_none),
        blocker=locator_result.blocker,
        reported_total=reported_total,
    )

    return plan, first_page_result


def clean_page(html: str, budget_tokens: int) -> CleanedPage:
    """Clean ``html`` into a :class:`CleanedPage` (thin wrapper over the cleaner).

    Mirrors the package's public :func:`app.extraction.clean` so the plan
    builder composes the same cleaning path without importing the package root
    (avoiding an import cycle at module load).
    """
    result = cleaner.build_clean_result(html)
    tokens = cleaner.count_tokens(result.lines)
    chunks = cleaner.chunk_lines(result.lines, result.lookup, budget_tokens)
    return CleanedPage(
        lines=result.lines,
        lookup=result.lookup,
        chunks=chunks,
        tokens=tokens,
    )
