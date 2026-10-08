"""Per-page extraction stage of the processing pipeline (review-analysis task 3.1).

After collection has made the dataset version's pages available under
``raw/v{n}/page-{k}.html`` (see :mod:`app.handlers.collection`), this stage
reads each captured page with the Extraction Engine using the Extraction Plan
saved with the dataset version, and records per page the method used, reviews
found, fallbacks, and discards (Requirement 3.2).

Scope (task 3.1): this stage *only* runs per-page extraction and collects the
details. It does not dedupe across pages (task 3.3), does not handle the upload
path (task 3.2), and does not compute metrics or write ``reviews/v{n}.json``
(task 5). It returns the combined reviews in page order (with their source page)
and the per-page details so those later stages can consume them.

Isolation (Requirement 3.1): the only inputs are the dataset's already-captured
pages (passed in as :class:`~app.handlers.collection.CapturedPage` objects, read
from the dataset's own S3 objects) and the saved plan. The AI provider is
reached only through the Extraction Engine's instrumented client; nothing else
is fetched. This stage adds no new data source.

Engineering rules honoured:

* **Stateless** — the stage holds no state beyond its inputs; all shared state
  lives in the captured pages (S3) and the saved plan.
* **Review text from elements** — :func:`app.extraction.extract_page` reads
  review text from page elements by code; the AI only points at elements. This
  stage never constructs or edits review text.
* **Reuse** — extraction is delegated wholesale to ``app.extraction``; nothing
  here re-implements cleaning, locating, selector reading, or pagination.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.extraction import extract_page as extraction_extract_page
from app.extraction.models import ExtractionPlan, Method, VerifiedReview
from app.handlers.collection import CapturedPage

logger = logging.getLogger(__name__)

# Discard-dict keys that are *not* discards and must be excluded from the
# per-page discard count. ``extract.py`` records ``structured_added`` in the
# same dict as the discard reasons for a single, complete accounting, but it is
# an *addition* (missed structured reviews added back), not a discard
# (review-extraction extract.py ``_KEY_STRUCTURED_ADDED``).
_NON_DISCARD_KEYS: frozenset[str] = frozenset({"structured_added"})


@dataclass(frozen=True)
class CollectedReview:
    """A Verified Review paired with the 1-based page it was extracted from.

    The source page is tracked here (rather than mutating the
    :class:`~app.extraction.models.VerifiedReview`) so the later dedupe and
    output stages can populate ``source_page`` in ``reviews/v{n}.json`` without
    this stage needing the output schema. Reviews are returned in page order
    (page 1 first), then in the order the Extraction Engine returned them within
    a page.
    """

    review: VerifiedReview
    source_page: int


@dataclass(frozen=True)
class PageDetail:
    """Per-page extraction details for one captured page (Requirement 3.2).

    Mirrors the ``pages`` array of ``reviews/v{n}.json`` (design Data Models):
    ``{page, url, method, found, fallback, discarded}``. ``discarded`` is the
    total number of items the chosen method dropped on this page (the sum of the
    Extraction Engine's per-reason discard counts, excluding the informational
    ``structured_added`` key, which records additions rather than discards).

    Attributes:
        page: 1-based page number (page 1 is the Check's first page).
        url: The page's final URL (as captured).
        method: The extraction method actually used — ``plan.method`` unless a
            page fell back to the Review Locator, in which case ``ai_direct``.
        found: Number of Verified Reviews the page yielded.
        fallback: Whether the page fell back to the Locator from its planned
            method.
        discarded: Total items dropped on this page by the chosen method.
        reported_total: The total review count the page itself showed (e.g. an
            aggregate "1,540 reviews"), when the Extraction Engine surfaced one;
            ``None`` otherwise. The metrics stage (task 5) uses the first page's
            value for completeness ("extracted vs reported", Requirement 5.4).
        structured_agreement: The share of structured-data reviews the chosen
            method also found on this page, when the page carried Structured
            Review Data to cross-check against (Requirement 5.6); ``None`` when
            the page had no structured data to agree with.
    """

    page: int
    url: str
    method: Method
    found: int
    fallback: bool
    discarded: int
    reported_total: int | None = None
    structured_agreement: float | None = None


@dataclass(frozen=True)
class ExtractionResult:
    """What the per-page extraction stage produced.

    Attributes:
        reviews: The combined Verified Reviews across all pages, each paired
            with its source page, in page order. Not yet deduped (task 3.3).
        pages: The per-page details, one per captured page, in page order.
    """

    reviews: list[CollectedReview] = field(default_factory=list)
    pages: list[PageDetail] = field(default_factory=list)


def _discard_total(discarded: dict[str, int]) -> int:
    """Sum the Extraction Engine's per-reason discard counts for one page.

    Excludes :data:`_NON_DISCARD_KEYS` (currently ``structured_added``), which
    the engine stores alongside the discard reasons but which counts additions,
    not discards. Negative or malformed values are ignored defensively; the
    engine only ever records non-negative counts.
    """
    return sum(
        count
        for reason, count in discarded.items()
        if reason not in _NON_DISCARD_KEYS and count > 0
    )


def extract_pages(
    pages: list[CapturedPage],
    plan: ExtractionPlan,
) -> ExtractionResult:
    """Extract every captured page with the saved plan (Requirement 3.1, 3.2).

    Calls :func:`app.extraction.extract_page` for each page in order, passing
    the plan and ``is_last=True`` only for the final page (which suppresses the
    Extraction Engine's low-yield fallback — a short last page is just short).
    Collects the Verified Reviews (tagged with their source page, in page order)
    and the per-page details (method used, reviews found, fallback, discards).

    This stage does not catch :class:`~app.extraction.errors.AIUnavailable` or
    :class:`~app.extraction.errors.LocatorUnavailable`; they propagate to the
    pipeline, which lets SQS retry with backoff (design Error Handling).

    Args:
        pages: The captured pages in order (page 1 first), from the collection
            stage. Each carries the rendered HTML read back from the dataset's
            own S3 objects.
        plan: The Extraction Plan saved with this dataset version.

    Returns:
        An :class:`ExtractionResult` with the combined reviews (not yet deduped)
        and the per-page details.
    """
    reviews: list[CollectedReview] = []
    details: list[PageDetail] = []

    last_index = len(pages) - 1
    for index, page in enumerate(pages):
        is_last = index == last_index
        result = extraction_extract_page(page.html, page.url, plan, is_last=is_last)

        for review in result.reviews:
            reviews.append(CollectedReview(review=review, source_page=page.page_num))

        details.append(
            PageDetail(
                page=page.page_num,
                url=page.url,
                method=result.method_used,
                found=len(result.reviews),
                fallback=result.fallback,
                discarded=_discard_total(result.discarded),
                reported_total=result.reported_total,
                structured_agreement=result.structured_agreement,
            )
        )
        logger.debug(
            "extracted page %d: method=%s found=%d fallback=%s discarded=%d",
            page.page_num,
            result.method_used,
            len(result.reviews),
            result.fallback,
            details[-1].discarded,
        )

    return ExtractionResult(reviews=reviews, pages=details)
