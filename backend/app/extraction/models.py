"""Data models for the Extraction Engine.

These mirror the structures described in the ``review-extraction`` design
document ("Data Models" and the Review Locator tool schema).  They are the
shared vocabulary between the cleaner, the Locator, post-processing, selector
validation, the plan builder, pagination, and per-page extraction.

All models are Pydantic v2 ``BaseModel`` subclasses so they validate at the
boundaries (notably the AI tool response) and serialize cleanly to the JSON
shapes stored in S3 and the database.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Shared literals
# ---------------------------------------------------------------------------

# Extraction method chosen for a site.
Method = Literal["selectors", "ai_direct", "structured"]

# Blockers the Locator can report when a page cannot be read.
Blocker = Literal["captcha", "login_wall", "consent_wall", "empty"]

# Confidence levels the Locator reports.
Confidence = Literal["low", "medium", "high"]

# Kinds the Locator assigns to items it points at.  Only ``review`` items are
# kept; the rest are counted as discards by reason.
ItemKind = Literal[
    "review",
    "qa",
    "seller_response",
    "owner_response",
    "editorial",
    "ad",
]


# ---------------------------------------------------------------------------
# Cleaner output
# ---------------------------------------------------------------------------


class CleanedPage(BaseModel):
    """A compact, reference-tagged view of a rendered page's visible content.

    ``lines`` are the rendered element lines (``e123 <div ...> "text" [attrs]``)
    in document order.  ``lookup`` maps each reference ID to a CSS path in the
    original DOM, so refs resolve back to real elements.  ``chunks`` holds the
    line groups the page was split into when it exceeded the token budget (a
    single chunk covering every line when it did not).  ``tokens`` is the token
    count of the full cleaned page.
    """

    lines: list[str] = Field(default_factory=list)
    lookup: dict[str, str] = Field(default_factory=dict)
    chunks: list[list[str]] = Field(default_factory=list)
    tokens: int = 0


# ---------------------------------------------------------------------------
# Locator (AI) output — mirrors the forced tool input schema
# ---------------------------------------------------------------------------


class LocatorItem(BaseModel):
    """One item the Locator points at, by element reference.

    The AI supplies references only; review text is read from the referenced
    elements by code.  ``rating_value`` is the AI's interpretation of a rating
    and is accepted only when the referenced item carries a rating cue and the
    value falls within the page's rating scale.
    """

    item_ref: str
    text_ref: str | None = None
    rating_value: float | None = None
    rating_ref: str | None = None
    date_ref: str | None = None
    author_ref: str | None = None
    title_ref: str | None = None
    kind: ItemKind = "review"


class ExcludedItem(BaseModel):
    """A non-review item the Locator deliberately excluded, with its kind."""

    ref: str
    kind: ItemKind


class LocatorSelectors(BaseModel):
    """CSS selectors the Locator suggests for reuse on similar pages."""

    item: str | None = None
    text: str | None = None
    rating: str | None = None
    date: str | None = None
    author: str | None = None
    title: str | None = None


class LocatorNextPage(BaseModel):
    """The element reference the Locator identified as the next-page control."""

    ref: str | None = None


class LocatorResult(BaseModel):
    """The schema-validated Review Locator response.

    Mirrors the tool input schema in the design.  Produced per Cleaned Page or
    per chunk; chunked results are merged by element reference.
    """

    has_reviews: bool = False
    blocker: Blocker | None = None
    rating_scale: float | None = None
    items: list[LocatorItem] = Field(default_factory=list)
    excluded_refs: list[ExcludedItem] = Field(default_factory=list)
    selectors: LocatorSelectors = Field(default_factory=LocatorSelectors)
    next_page: LocatorNextPage = Field(default_factory=LocatorNextPage)
    reported_total: int | None = None
    entity_hint: str | None = None
    confidence: Confidence | None = None


# ---------------------------------------------------------------------------
# Verified reviews and structured data
# ---------------------------------------------------------------------------


class VerifiedReview(BaseModel):
    """A review whose text was read by code from a page element and validated.

    ``source_ref`` is the reference ID (or structured-data locator) the review
    was read from, kept for traceability.
    """

    text: str
    rating: float | None = None
    date: str | None = None
    author: str | None = None
    title: str | None = None
    source_ref: str | None = None


class StructuredResult(BaseModel):
    """JSON-LD / microdata reviews that passed the visible-text check.

    ``review_count`` is the ``AggregateRating.reviewCount`` reported by the
    page when present.
    """

    reviews: list[VerifiedReview] = Field(default_factory=list)
    review_count: int | None = None


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class NextPage(BaseModel):
    """The next-page outcome for a page.

    ``url`` is the resolved absolute URL when one exists; ``rule_used`` names
    the rule that produced it; ``reason_if_none`` explains why no URL exists
    (for example script-driven infinite scroll).
    """

    url: str | None = None
    rule_used: str
    reason_if_none: str | None = None


class NextPageRule(BaseModel):
    """A reusable next-page rule stored in an Extraction Plan.

    ``type`` is ``"selector"`` with a ``css`` value, ``"url_template"`` with a
    ``template`` containing a page-number placeholder, or ``"none"`` when the
    page offers no URL-based next page.
    """

    type: Literal["selector", "url_template", "none"]
    css: str | None = None
    template: str | None = None


# ---------------------------------------------------------------------------
# Extraction plan
# ---------------------------------------------------------------------------


class FirstPageStats(BaseModel):
    """First-page counts recorded in an Extraction Plan."""

    verified: int = 0
    discarded: int = 0
    structured_count: int = 0
    per_page_rate: int = 0


class ExtractionPlan(BaseModel):
    """The saved result of locating reviews on a first page.

    Mirrors the plan JSON in the design: the chosen method, validated selectors
    (if any), the rating scale, the next-page rule, first-page statistics, and
    provenance (Locator model, prompt version).  ``degraded`` is ``True`` when
    the plan was built from Structured Review Data alone because the AI was
    unavailable.
    """

    version: int = 1
    created_at: str
    locator_model: str | None = None
    prompt_version: str | None = None
    method: Method
    selectors: LocatorSelectors = Field(default_factory=LocatorSelectors)
    rating_scale: float | None = None
    next_page_rule: NextPageRule
    first_page: FirstPageStats = Field(default_factory=FirstPageStats)
    reported_total: int | None = None
    entity_hint: str | None = None
    confidence: Confidence | None = None
    degraded: bool = False


# ---------------------------------------------------------------------------
# Per-page extraction result
# ---------------------------------------------------------------------------


class PageResult(BaseModel):
    """The result of extracting a single page.

    Includes the Verified Reviews, the method actually used, whether a fallback
    happened, the discard counts by reason, the structured agreement rate when
    it applies, the next-page candidate, any blocker, and the reported total.
    """

    reviews: list[VerifiedReview] = Field(default_factory=list)
    method_used: Method
    fallback: bool = False
    discarded: dict[str, int] = Field(default_factory=dict)
    structured_agreement: float | None = None
    next_page: NextPage
    blocker: Blocker | None = None
    reported_total: int | None = None
