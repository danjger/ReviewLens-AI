"""Page collection stage of the processing pipeline (review-analysis task 2).

For a URL dataset the Worker walks the review listing one page at a time,
starting from the first page captured during the Check. For each step it asks
the Extraction Engine where the list continues (:func:`app.extraction.next_page`),
SSRF-checks the candidate, renders it with the browser (:func:`app.capture.render`),
and stores the rendered HTML under the dataset's permanent ``raw/v{n}`` prefix.
Collection stops at the first of these limits (Requirement 2.1):

* **no next page** — the engine reports no URL-based next page; when a next /
  "Load more" control exists but carries no URL, a *script-only pagination*
  warning is recorded with the reason (Requirement 2.7);
* **MAX_PAGES** — the configured page cap (default 10);
* **MAX_REVIEWS** — the running review estimate reaches the cap (default 1000);
* **time budget** — collection approaches the soft budget (default 12 min) so
  later stages still fit inside the 15-minute Lambda budget (design Error
  Handling).

Other behaviours required by Requirement 2:

* **Progress events** — one ``log_event`` per captured page, e.g. "Captured
  page 3 of up to 10" (Requirement 2.2); through :mod:`app.db.status` so it is
  published for the live UI.
* **Page failure** — if a later page fails to load, collection stops, records a
  warning, and the pipeline continues with the pages already gathered
  (Requirement 2.3). The first page is never collected here (it came from the
  Check), so a failure only ever loses *later* pages.
* **Per-host delay** — at least ``PAGE_REQUEST_DELAY_S`` between requests to the
  same host (Requirement 2.4).
* **Uploads** — collection is skipped entirely for upload datasets
  (Requirement 2.5); the caller does not invoke this stage for them, and
  :func:`collect_pages` also guards defensively.
* **SSRF** — every candidate URL passes ``assert_public_host`` before it is
  fetched (Requirement 2.6).

Idempotency (Requirement 7.3): pages are keyed by version and page number via
:mod:`app.storage.keys`, and a page whose object already exists is reused rather
than re-rendered, so a retry after a crash does not re-capture pages. Because
the first page's object is written by Add/Refresh before processing starts, the
loop reads it back rather than assuming it is in memory.

Engineering rules honoured:

* **Stateless** — all state is the dataset's S3 objects and the dataset row; the
  only per-invocation state is the injected :class:`TimeBudget`.
* **Keys** — every S3 key comes from :mod:`app.storage.keys`.
* **SSRF** — ``assert_public_host`` from ``dataset-ingestion`` guards every fetch.
* **Status** — progress and warnings go through :mod:`app.db.status`.

The browser render, the clock, and the sleep are module-level indirections so
unit tests can substitute fakes without a real browser or wall-clock waits; the
defaults are the real :func:`app.capture.render`, :func:`time.monotonic`, and
:func:`time.sleep`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.core.config import get_settings
from app.db.status import log_event
from app.extraction import next_page as extraction_next_page
from app.extraction.models import ExtractionPlan
from app.handlers._timebudget import TimeBudget
from app.ingestion.url_validator import SsrfError, assert_public_host
from app.storage import keys, s3

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result of the collection stage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapturedPage:
    """One page the collection stage made available for extraction.

    Attributes:
        page_num: 1-based page number (page 1 is the Check's captured first
            page).
        url: The page's URL — the Final URL for page 1, otherwise the resolved
            next-page URL that was fetched.
        html: The rendered HTML read back from S3.
        key: The S3 key the HTML is stored under (always from
            :mod:`app.storage.keys`).
        reused: ``True`` when the page's object already existed and was reused
            rather than re-rendered (idempotent retry, Requirement 7.3).
    """

    page_num: int
    url: str
    html: str
    key: str
    reused: bool


@dataclass(frozen=True)
class CollectionResult:
    """What the collection stage gathered and why it stopped.

    Attributes:
        pages: The captured pages in order, page 1 first. Always at least the
            first page for a URL dataset.
        warnings: Human-readable warnings to carry into the metrics (a page
            failure, a script-only pagination notice, or an early stop for the
            time budget). Each is also logged as a progress event.
        stop_reason: Why collection stopped — one of :data:`STOP_NO_NEXT_PAGE`,
            :data:`STOP_MAX_PAGES`, :data:`STOP_MAX_REVIEWS`,
            :data:`STOP_TIME_BUDGET`, :data:`STOP_PAGE_FAILURE`, or
            :data:`STOP_SKIPPED_UPLOAD`.
    """

    pages: list[CapturedPage]
    warnings: list[str] = field(default_factory=list)
    stop_reason: str = ""


# Stop-reason vocabulary (also the loop's exit points, each unit-tested).
STOP_NO_NEXT_PAGE = "no_next_page"
STOP_MAX_PAGES = "max_pages"
STOP_MAX_REVIEWS = "max_reviews"
STOP_TIME_BUDGET = "time_budget"
STOP_PAGE_FAILURE = "page_failure"
STOP_SKIPPED_UPLOAD = "skipped_upload"


# ---------------------------------------------------------------------------
# Module-level indirections (overridable in tests)
# ---------------------------------------------------------------------------


def _default_render(url: str, key: str) -> tuple[str, str]:
    """Render *url* with the browser, store its HTML under *key*, return (html, final_url).

    Delegates to :func:`app.capture.render` (workers image only). Imported
    lazily so this module can be imported in the base image for type-checking
    without pulling in Playwright; the render only happens at call time in the
    workers image. The capture engine writes ``{prefix}page.html`` under the
    prefix it is given, so it is pointed at a per-page prefix derived from the
    canonical key; the rendered HTML is read back and re-stored at the canonical
    ``raw/v{n}/page-{k}.html`` key (always from :mod:`app.storage.keys`) so later
    stages read a predictable layout.

    Returns the rendered HTML and the browser's **final** URL (which differs from
    *url* on a client-side redirect); the final URL is what the Extraction Engine
    must see for the next ``next_page`` call.
    """
    from app.capture.engine import render  # noqa: PLC0415 - workers-image only

    prefix = key.rsplit("/", 1)[0] + "/"
    result = render(url, prefix)
    html = s3.get_text(result.html_key)
    if result.html_key != key:
        s3.put_bytes(key, html.encode("utf-8"), content_type="text/html; charset=utf-8")
    return html, result.final_url


#: The browser renderer. Signature ``(url, canonical_key) -> (html, final_url)``.
#: Overridable in tests to avoid launching Chromium.
render_page = _default_render

#: Monotonic clock used for the per-host delay. Overridable in tests.
monotonic = time.monotonic

#: Sleep used to honour the per-host delay. Overridable in tests so no real
#: wall-clock time passes.
sleep = time.sleep


# ---------------------------------------------------------------------------
# Review-estimate hook (filled by extraction stage; conservative default here)
# ---------------------------------------------------------------------------


def _estimate_reviews_per_page(plan: ExtractionPlan) -> int:
    """Estimate reviews per captured page from the plan's first-page stats.

    The MAX_REVIEWS loop limit (Requirement 2.1) needs a *running* review count
    while collecting, before extraction has run. The plan recorded how many
    reviews the first page verified, which is the best cheap estimate of what a
    further page will yield. A floor of 1 keeps the estimate from stalling the
    limit when the first page recorded zero verified reviews.
    """
    per_page = plan.first_page.verified
    return max(1, per_page)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def collect_pages(
    dataset_id: str,
    version: int,
    plan: ExtractionPlan | None,
    first_page_url: str,
    budget: TimeBudget,
    *,
    is_upload: bool = False,
) -> CollectionResult:
    """Collect review pages for a dataset version (Requirement 2).

    Starts from page 1 (the Check's captured page, read back from S3) and walks
    the listing with :func:`app.extraction.next_page` →
    :func:`app.capture.render`, SSRF-checking and rate-limiting each fetch, until
    one of the loop limits is hit (see the module docstring). Pages already
    captured for this version are reused, not re-rendered (Requirement 7.3). A
    later-page failure stops collection with a warning and keeps the pages
    already gathered (Requirement 2.3). Upload datasets skip the stage entirely
    (Requirement 2.5).

    Args:
        dataset_id: The dataset being processed.
        version: The data version being processed; every page key includes it.
        plan: The Extraction Plan saved with this version (drives the next-page
            rule and the review-per-page estimate).
        first_page_url: The Final URL of page 1 (candidates are resolved against
            it and SSRF-checked relative to it).
        budget: The invocation's :class:`TimeBudget`; collection stops as the
            soft collection budget is approached.
        is_upload: When ``True``, collection is skipped (defensive; callers do
            not invoke this stage for uploads).

    Returns:
        A :class:`CollectionResult` with the captured pages, any warnings, and
        the stop reason.
    """
    if is_upload:
        # Requirement 2.5: uploads have no pages to collect.
        return CollectionResult(pages=[], warnings=[], stop_reason=STOP_SKIPPED_UPLOAD)

    if plan is None:  # pragma: no cover - URL datasets always carry a plan
        raise ValueError("A URL dataset must have an Extraction Plan to collect pages")

    settings = get_settings()
    max_pages: int = settings.max_pages
    max_reviews: int = settings.max_reviews
    delay_s: float = settings.page_request_delay_s
    collection_budget_s: float = float(settings.collection_time_budget_s)

    per_page_estimate = _estimate_reviews_per_page(plan)

    # Page 1 came from the Check; read it back from S3 (stateless — nothing is
    # assumed to be in memory across stages).
    page1_key = keys.dataset_raw_page(dataset_id, version, 1)
    html = s3.get_text(page1_key)
    url = first_page_url
    pages: list[CapturedPage] = [
        CapturedPage(page_num=1, url=url, html=html, key=page1_key, reused=True)
    ]
    warnings: list[str] = []

    # Running review estimate, used only to decide the MAX_REVIEWS exit.
    review_estimate = per_page_estimate

    # Track the last host and when it was last fetched so the per-host delay is
    # applied only when the next fetch targets the same host (Requirement 2.4).
    last_host = urlsplit(url).hostname
    last_fetch_monotonic = monotonic()

    stop_reason = STOP_NO_NEXT_PAGE
    for page_num in range(2, max_pages + 1):
        # Stop before fetching if the soft collection budget is spent, leaving
        # the rest of the Lambda budget for later stages (design Error Handling).
        if budget.elapsed() >= collection_budget_s or budget.expired():
            message = f"Stopped collecting after {len(pages)} page(s): time budget reached"
            warnings.append(message)
            log_event(dataset_id, message, extra={"stop_reason": STOP_TIME_BUDGET})
            stop_reason = STOP_TIME_BUDGET
            break

        nxt = extraction_next_page(html, url, plan)
        if not nxt.url:
            # Requirement 2.7: a next/"Load more" control that carries no URL
            # (script-driven) is recorded as a warning with its reason.
            if nxt.reason_if_none == "script_driven_no_url":
                message = "More reviews load only by script; collected pages may be incomplete"
                warnings.append(message)
                log_event(
                    dataset_id,
                    message,
                    extra={"stop_reason": STOP_NO_NEXT_PAGE, "reason": nxt.reason_if_none},
                )
            stop_reason = STOP_NO_NEXT_PAGE
            break

        next_url = nxt.url

        # Requirement 2.6: SSRF-check every page before it is fetched. A blocked
        # candidate is treated like a page failure: stop, warn, keep what we have.
        try:
            assert_public_host(next_url)
        except SsrfError as exc:
            message = f"Stopped collecting at page {page_num}: address not allowed"
            warnings.append(message)
            log_event(dataset_id, message, extra={"stop_reason": STOP_PAGE_FAILURE})
            logger.warning("collection SSRF-blocked next page", extra={"blocked": str(exc)})
            stop_reason = STOP_PAGE_FAILURE
            break

        # Requirement 7.3: reuse a page already captured for this version rather
        # than re-rendering it (idempotent retry after a crash).
        key = keys.dataset_raw_page(dataset_id, version, page_num)
        reused = s3.object_exists(key)

        try:
            if reused:
                page_html = s3.get_text(key)
                final_url = next_url
            else:
                page_html, final_url = _capture_page(
                    dataset_id,
                    version,
                    page_num,
                    next_url,
                    host_delay=_host_delay(next_url, last_host, last_fetch_monotonic, delay_s),
                )
                last_host = urlsplit(final_url).hostname
                last_fetch_monotonic = monotonic()
        except Exception as exc:  # noqa: BLE001 - any render failure stops collection
            # Requirement 2.3: a later page failing stops collection with a
            # warning; the pipeline continues with the pages already gathered.
            message = f"Stopped collecting at page {page_num}: page failed to load"
            warnings.append(message)
            log_event(dataset_id, message, extra={"stop_reason": STOP_PAGE_FAILURE})
            logger.info("collection page %d failed: %s", page_num, exc)
            stop_reason = STOP_PAGE_FAILURE
            break

        pages.append(
            CapturedPage(page_num=page_num, url=final_url, html=page_html, key=key, reused=reused)
        )
        # Requirement 2.2: a progress event per captured page.
        log_event(
            dataset_id,
            f"Captured page {page_num} of up to {max_pages}",
            extra={"page": page_num, "max_pages": max_pages},
        )

        # Advance for the next iteration.
        html = page_html
        url = final_url

        # Requirement 2.1: stop once the running review estimate reaches the cap.
        review_estimate += per_page_estimate
        if review_estimate >= max_reviews:
            stop_reason = STOP_MAX_REVIEWS
            break
    else:
        # The for-loop ran to completion without breaking: MAX_PAGES reached.
        stop_reason = STOP_MAX_PAGES

    return CollectionResult(pages=pages, warnings=warnings, stop_reason=stop_reason)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _host_delay(
    next_url: str,
    last_host: str | None,
    last_fetch_monotonic: float,
    delay_s: float,
) -> float:
    """Return how long to sleep before fetching *next_url* (Requirement 2.4).

    When the next request targets the **same host** as the previous fetch, wait
    out the remainder of ``delay_s`` since that fetch; a request to a different
    host (which a cross-subdomain pagination may produce) is not delayed. Never
    returns a negative value.
    """
    if urlsplit(next_url).hostname != last_host:
        return 0.0
    waited = monotonic() - last_fetch_monotonic
    return max(0.0, delay_s - waited)


def _capture_page(
    dataset_id: str,
    version: int,
    page_num: int,
    next_url: str,
    *,
    host_delay: float,
) -> tuple[str, str]:
    """Render *next_url* into ``raw/v{n}/page-{k}.html`` and return (html, final_url).

    Applies the per-host delay first (Requirement 2.4), then renders with the
    browser via :data:`render_page`, which stores the HTML at the canonical
    ``raw/v{n}/page-{k}.html`` key (from :mod:`app.storage.keys`) and returns the
    rendered HTML and the browser's final URL. The final URL (not ``next_url``)
    is returned so a client-side redirect is reflected in the next
    ``next_page`` call.
    """
    if host_delay > 0:
        sleep(host_delay)

    key = keys.dataset_raw_page(dataset_id, version, page_num)
    html, final_url = render_page(next_url, key)
    return html, final_url
