"""Review Extraction Engine (``app.extraction``).

A pure library that turns rendered HTML into Verified Reviews, an Extraction
Plan, and next-page candidates, with no network access of its own except AI
calls through the instrumented Claude client.  It is used by the check handler
(``dataset-ingestion``) to build a plan and predict viability, and by the
processing handler (``review-analysis``) to extract every captured page.

This module is the package's public API.  The concrete implementations live in
submodules (``cleaner``, ``structured``, ``locator``, ``postprocess``,
``selectors``, ``plan``, ``pagination``) and are wired up by later tasks; the
functions here are the stable entry points callers import.

Review detection is AI-first: the Locator points at elements, and review text
is always read from the page by code — never written by the AI.
"""

from __future__ import annotations

from app.extraction.errors import AIUnavailable, LocatorUnavailable
from app.extraction.models import (
    CleanedPage,
    ExcludedItem,
    ExtractionPlan,
    FirstPageStats,
    LocatorItem,
    LocatorNextPage,
    LocatorResult,
    LocatorSelectors,
    NextPage,
    NextPageRule,
    PageResult,
    StructuredResult,
    VerifiedReview,
)

__all__ = [
    # Errors
    "AIUnavailable",
    "LocatorUnavailable",
    # Models
    "CleanedPage",
    "ExcludedItem",
    "ExtractionPlan",
    "FirstPageStats",
    "LocatorItem",
    "LocatorNextPage",
    "LocatorResult",
    "LocatorSelectors",
    "NextPage",
    "NextPageRule",
    "PageResult",
    "StructuredResult",
    "VerifiedReview",
    # Public API
    "clean",
    "parse_structured",
    "locate",
    "build_plan",
    "extract_page",
    "next_page",
]


def clean(html: str, budget_tokens: int) -> CleanedPage:
    """Reduce rendered HTML to a reference-tagged Cleaned Page.

    Removes scripts, styles, hidden elements, and other non-content, keeps
    visible text and rating-bearing attributes, assigns a stable reference ID
    to every kept block element, and splits the page into chunks when it
    exceeds ``budget_tokens``.

    :param html: Rendered HTML of the page.
    :param budget_tokens: Token budget above which the page is trimmed and
        chunked.
    :returns: The Cleaned Page with its ref-to-element lookup and chunks.
    """
    from app.extraction import cleaner

    result = cleaner.build_clean_result(html)
    tokens = cleaner.count_tokens(result.lines)
    chunks = cleaner.chunk_lines(result.lines, result.lookup, budget_tokens)
    return CleanedPage(
        lines=result.lines,
        lookup=result.lookup,
        chunks=chunks,
        tokens=tokens,
    )


def parse_structured(html: str, visible_text: str) -> StructuredResult:
    """Read embedded JSON-LD and microdata review data from a page.

    Walks ``Review`` objects nested under the supported container types and
    keeps only reviews whose text also appears in ``visible_text`` after
    normalization.  Also reads the ``AggregateRating`` review count.

    :param html: Rendered HTML of the page.
    :param visible_text: The page's normalized visible text, used to verify
        each structured review.
    :returns: The verified structured reviews and the reported review count.
    """
    from app.extraction import structured

    return structured.parse_structured(html, visible_text)


def locate(page: CleanedPage, *, url: str, title: str) -> LocatorResult:
    """Run the Review Locator (an AI call) over a Cleaned Page.

    Returns element references for each review and its fields, suggested CSS
    selectors, the next-page element, the reported total, any blocker, an
    entity hint, and a confidence level.  Chunked pages are located per chunk
    and merged by reference.

    :param page: The Cleaned Page (or chunk) to read.
    :param url: The page's URL, for context in the prompt.
    :param title: The page's title, for context in the prompt.
    :returns: The schema-validated Locator result.
    :raises AIUnavailable: The AI provider is unavailable or the global AI
        limit was reached.
    :raises LocatorUnavailable: The response failed schema validation and the
        repair retry also failed.
    """
    from app.extraction import locator

    return locator.locate(page, url=url, title=title)


def build_plan(html: str, url: str, title: str) -> tuple[ExtractionPlan, PageResult]:
    """Build an Extraction Plan from a first page.

    Called during a URL check.  Cleans the page, locates reviews, validates
    suggested selectors, cross-checks structured data, chooses the cheapest
    reliable method, derives the next-page rule, and returns the plan alongside
    the first page's extraction result.

    :param html: Rendered HTML of the first page.
    :param url: The page's final URL.
    :param title: The page's title.
    :returns: The Extraction Plan and the first-page ``PageResult``.
    When the AI provider is unavailable, this does not raise: it builds a
    structured-only degraded plan (``degraded=True``) from embedded Structured
    Review Data so callers can degrade gracefully (Requirement 4.4).

    :raises LocatorUnavailable: The Locator could not produce a valid response
        (a hard retryable error; it propagates rather than degrading).
    """
    from app.extraction import plan as _plan

    return _plan.build_plan(html, url, title)


def extract_page(
    html: str,
    url: str,
    plan: ExtractionPlan,
    *,
    is_last: bool,
) -> PageResult:
    """Extract one page according to its Extraction Plan, with fallback.

    Dispatches by the plan's method, falls back to the Review Locator when a
    selector-based page yields too few reviews (and it is not the last page) or
    when structured data is absent, cross-checks structured data, and reports
    the method actually used and whether a fallback happened.

    :param html: Rendered HTML of the page.
    :param url: The page's final URL.
    :param plan: The Extraction Plan to follow.
    :param is_last: Whether the caller believes this is the last page (which
        suppresses the low-yield fallback).
    :returns: The page's extraction result.
    :raises AIUnavailable: The AI provider is unavailable during a fallback.
    :raises LocatorUnavailable: The Locator could not produce a valid response.
    """
    from app.extraction import extract

    return extract.extract_page(html, url, plan, is_last=is_last)


def next_page(
    html: str,
    url: str,
    plan: ExtractionPlan | None,
    locator: LocatorResult | None = None,
) -> NextPage:
    """Resolve the next-page candidate for a page.

    Applies, in order, the plan's next-page rule, generic patterns, and the
    Locator's next-page element.  Candidates are resolved to absolute URLs and
    filtered to the page's registrable domain; when no URL-based next page
    exists, the reason is reported.

    :param html: Rendered HTML of the page.
    :param url: The page's final URL.
    :param plan: The Extraction Plan, when one exists.
    :param locator: The Locator result for this page, when it was read by the
        Locator.
    :returns: The next-page outcome.
    """
    from app.extraction import pagination

    return pagination.next_page(html, url, plan, locator)
