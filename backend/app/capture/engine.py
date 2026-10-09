"""Headless-browser page capture (workers image only).

``render(url, prefix)`` renders a URL in headless Chromium, saves the rendered
HTML and an above-the-fold screenshot to S3 under *prefix*, reads the page
title, and reports the main document's response status and the browser's final
URL. It is the Capture step of the Check pipeline (design: "the in-process
Capture module").

Requirements covered:

* **1.3 / 2.7** — every request the browser makes is SSRF-checked by a
  ``page.route("**/*")`` guard; a blocked sub-request (for example an image
  loaded from ``127.0.0.1``) is aborted, and the main document itself is
  SSRF-checked before navigation.
* **2.8** — when the page redirects itself (meta refresh or script redirect),
  the browser ends on a different URL; :func:`render` returns ``final_url =
  page.url`` so the caller can record a ``client_redirect`` hop.
* **3.1 / 4.1** — the page is rendered in a headless browser with a 1280×900
  viewport; the rendered HTML and a PNG screenshot are stored in the Check
  Session area in S3.
* **4.3** — the page ``<title>`` is read and returned as ``page_title``.

Engineering rules honoured here:

* **Same code, both compute modes.** This module imports no Lambda event
  shapes; it is driven by the check handler, which runs identically under the
  Lambda SQS adapter and the container poller.
* **Stateless.** No process-level browser state is kept: each :func:`render`
  call owns its Playwright lifecycle for that call only, so no cookies,
  storage, or cache leak between captures. Nothing on local disk is relied on;
  S3 is the store.
* **SSRF.** Outbound fetches of the user-supplied URL — the navigation and
  every browser sub-request — pass :func:`assert_public_host`.
* **Keys.** Object keys are built only from the supplied *prefix*, which the
  caller derives from :mod:`app.storage.keys`.

Playwright's *synchronous* API is used so this module runs in the same
synchronous call path as the probe (:mod:`app.ingestion.url_validator`) without
requiring an event loop in the handler.

Thread safety
-------------
Playwright's sync API is **not** thread-safe: a ``sync_playwright()`` object is
bound to the thread (greenlet) that created it and cannot be driven from
another thread. The queue consumer runs each handler alongside a per-message
*heartbeat thread* and reuses the process across messages, so a browser cached
in a module global and reused across calls eventually gets driven from a
different thread context and raises ``greenlet.error: cannot switch to a
different thread``. To avoid that entirely, every :func:`render` call runs its
whole Playwright session — start, launch, navigate, capture, teardown — inside
a *dedicated worker thread* that creates and destroys its own
``sync_playwright()`` instance. The Playwright object is therefore created and
used on exactly one thread and never switched, regardless of which thread calls
:func:`render`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Request, Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.ingestion.url_validator import SsrfError, assert_public_host

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Viewport used for rendering and the above-the-fold screenshot (Req 4.1).
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 900

#: Cap on waiting for network to go idle, in milliseconds. Pages that keep a
#: long-poll or analytics socket open never reach ``networkidle``; the cap
#: keeps a single capture bounded well inside ``CHECK_TIMEOUT_S`` (Req 3.1).
NETWORKIDLE_TIMEOUT_MS = 15_000

#: Timeout for the initial navigation, in milliseconds. Kept below the
#: networkidle cap so a hung first byte fails fast rather than near the
#: per-URL budget.
NAVIGATION_TIMEOUT_MS = 15_000

#: Chrome ``User-Agent`` presented by the browser, matching the probe's
#: headers so a site judges capture the same way the probe reached it
#: (consistent with Requirement 2.6).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: Chromium launch flags, split into two concerns.
#:
#: STABILITY (Lambda/container): headless Chromium must run inside the AWS
#: Lambda sandbox where there is no usable ``/dev/shm``, no GPU, and the only
#: writable path is ``/tmp``. Without these flags ``launch()`` succeeds but the
#: first ``new_page()`` hangs ~30s and errors (the renderer/zygote can't start)
#: — the heavy-page capture failure seen on judge.me. ``--no-sandbox`` is
#: required (no user namespaces in the sandbox); ``--disable-dev-shm-usage``
#: moves shared memory off the tiny ``/dev/shm``; ``--no-zygote`` +
#: ``--disable-gpu`` + ``--disable-software-rasterizer`` avoid the GPU/zygote
#: processes that don't come up here.
_CHROMIUM_STABILITY_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--no-zygote",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-background-networking",
    "--disable-extensions",
    # Playwright manages its own (temp) profile dir, so we do NOT pass
    # --user-data-dir (Playwright rejects it). The crash-dumps dir just needs to
    # be writable; /tmp is the only writable mount on Lambda (scratch only).
    "--crash-dumps-dir=/tmp/chromium-crashes",
]

#: STEALTH: reduce the most obvious headless-automation fingerprints (the
#: ``AutomationControlled`` blink feature sets ``navigator.webdriver`` and trips
#: lazy bot checks). These do NOT defeat commercial anti-bot
#: (Cloudflare/Akamai); they only help legitimately-public pages behind
#: lightweight checks render instead of 403ing. See review-extraction design
#: "Capture stealth" / Known Issues.
_STEALTH_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
]

#: Realistic request headers to accompany the Chrome UA, so a site judges the
#: capture the way a normal browser visit would (consistent with Req 2.6).
_EXTRA_HTTP_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

#: Init script injected before any page script runs, masking the two most-
#: checked automation tells: ``navigator.webdriver`` and an empty plugins list.
_STEALTH_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    "Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});"
    "Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});"
)

#: Filenames stored under the caller-supplied prefix. These mirror the
#: trailing components produced by ``storage.keys.check_page`` /
#: ``check_snapshot`` so the layout stays in one place conceptually.
_PAGE_FILENAME = "page.html"
_SNAPSHOT_FILENAME = "snapshot.png"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureResult:
    """The outcome of rendering and capturing one page.

    Attributes:
        final_url: ``page.url`` after rendering. Differs from the requested URL
            when the page performed a client-side redirect (Req 2.8).
        main_status: HTTP status of the *main document* response. ``None`` when
            no main response was observed (rare; e.g. a ``data:`` navigation).
            The caller gives a non-200 main status a ``wont_work`` verdict
            (Req 2.7).
        page_title: The page ``<title>`` text (Req 4.3). Empty string when the
            page has no title.
        html_key: S3 key of the stored rendered HTML.
        snapshot_key: S3 key of the stored above-the-fold PNG screenshot.
        redirected: True when ``final_url`` differs from the requested URL.
    """

    final_url: str
    main_status: int | None
    page_title: str
    html_key: str
    snapshot_key: str
    redirected: bool


# ---------------------------------------------------------------------------
# Blocked-request error carried out of the route handler
# ---------------------------------------------------------------------------


class CaptureBlockedError(Exception):
    """Raised when the *main navigation* targets a disallowed address.

    A blocked *sub-request* is merely aborted and the render continues; but if
    the page the analyst asked for is itself non-public, there is nothing to
    capture and the check must fail with "Address not allowed" (Req 1.3).
    """


# ---------------------------------------------------------------------------
# Browser lifecycle hooks (no shared process-level browser)
# ---------------------------------------------------------------------------
#
# There is deliberately no cached per-process browser any more: a sync
# Playwright object bound to the thread that created it cannot be driven from
# another thread, and the consumer reuses the process (and runs a per-message
# heartbeat thread) across messages. Each :func:`render` call instead owns its
# own Playwright instance on its own worker thread (see :func:`_render_blocking`).
#
# ``_get_browser`` and ``shutdown_browser`` are kept as explicit hooks so
# existing callers (consumer shutdown) and tests (which assert a blocked main
# URL never launches a browser, and clean up between cases) keep working
# unchanged.


def _get_browser() -> None:
    """Launch-point hook, retained for compatibility.

    No browser is cached at the process level any more; a browser is launched
    inside :func:`render`'s dedicated worker thread for the lifetime of that
    call. This hook is invoked by :func:`render` only *after* the main URL has
    passed the SSRF check, so tests can monkeypatch it to assert that a blocked
    main URL never reaches a browser launch.
    """
    return None


def shutdown_browser() -> None:
    """No-op worker/test shutdown hook.

    Previously closed the shared per-process browser; now each capture tears
    its own Playwright instance down when :func:`render` returns, so there is
    nothing process-level to close. Kept so consumer shutdown and test teardown
    callers continue to work. Safe to call at any time.
    """
    return None


# ---------------------------------------------------------------------------
# SSRF route guard
# ---------------------------------------------------------------------------


def _route_guard(route: Route, request: Request) -> None:
    """Abort any browser request whose host is not a public address (Req 1.3).

    Every request the page makes — the document, scripts, stylesheets, images,
    XHR/fetch — is resolved and SSRF-checked. Blocked requests are aborted so
    the page cannot be used to reach an internal address (design: the capture
    route guard "blocks a page that loads an image from 127.0.0.1").
    """
    try:
        assert_public_host(request.url)
    except SsrfError:
        logger.warning("Capture aborted blocked request", extra={"request_url": request.url})
        route.abort("blockedbyclient")
        return
    route.continue_()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def render(url: str, prefix: str) -> CaptureResult:
    """Render *url* in headless Chromium and capture it under *prefix*.

    Steps:

    1. SSRF-check the requested URL before touching the browser (Req 1.3).
    2. On a dedicated worker thread, start Playwright, launch headless Chromium,
       and open a fresh 1280×900 context isolating this capture (Req 3.1). The
       whole session lives and dies on that one thread (see module docstring).
    3. Install the ``page.route("**/*")`` SSRF guard on every request.
    4. Navigate, wait for ``networkidle`` (capped at 15 s), then scroll once to
       trigger lazy-loaded reviews.
    5. Save the rendered HTML and a PNG screenshot to S3 under *prefix*, and
       read the ``<title>``.
    6. Return the main document's response status and the browser's final URL.

    Args:
        url: The Final URL (post-probe) to render.
        prefix: The S3 key prefix for this capture, ending in ``/`` (for
            example ``checks/{check_id}/{item_id}/``). HTML is stored at
            ``{prefix}page.html`` and the screenshot at ``{prefix}snapshot.png``.

    Returns:
        A :class:`CaptureResult`.

    Raises:
        CaptureBlockedError: when the requested URL's host is not public.
        PlaywrightTimeoutError: when the initial navigation times out.
    """
    # Imported here (not at module top) so this module can be imported in the
    # base image for type checking without importing S3 at definition time;
    # the write itself only happens in the workers image at call time.
    from app.storage import s3

    # Guard the main navigation up front: a non-public target is refused before
    # a browser is even launched (Req 1.3).
    try:
        assert_public_host(url)
    except SsrfError as exc:
        raise CaptureBlockedError(str(exc)) from exc

    # Compatibility hook: invoked only after the SSRF check so tests can assert
    # a blocked main URL never reaches a browser launch (see _get_browser).
    _get_browser()

    html_key = f"{prefix}{_PAGE_FILENAME}"
    snapshot_key = f"{prefix}{_SNAPSHOT_FILENAME}"

    # Run the entire Playwright session on a dedicated worker thread so the
    # sync Playwright object is created and used on exactly one thread and is
    # never switched to another (the heartbeat thread / process reuse would
    # otherwise trigger `greenlet.error: cannot switch to a different thread`).
    rendered = _run_on_dedicated_thread(url)

    s3.put_bytes(html_key, rendered.html.encode("utf-8"), content_type="text/html; charset=utf-8")
    s3.put_bytes(snapshot_key, rendered.screenshot, content_type="image/png")

    return CaptureResult(
        final_url=rendered.final_url,
        main_status=rendered.main_status,
        page_title=rendered.title,
        html_key=html_key,
        snapshot_key=snapshot_key,
        redirected=rendered.final_url != url,
    )


@dataclass(frozen=True)
class _Rendered:
    """Raw render output carried back from the Playwright worker thread.

    S3 writes happen on the calling thread (not the Playwright thread) so the
    worker thread owns nothing but the browser session.
    """

    html: str
    title: str
    screenshot: bytes
    main_status: int | None
    final_url: str


def _run_on_dedicated_thread(url: str) -> _Rendered:
    """Render *url* on a fresh thread that owns its own Playwright instance.

    Blocks until the worker thread finishes, then returns its result or
    re-raises whatever it raised (notably :class:`PlaywrightTimeoutError` on a
    hung navigation) on the calling thread, so :func:`render`'s contract is
    unchanged.
    """
    result: dict[str, _Rendered] = {}
    error: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            result["value"] = _render_blocking(url)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            error["value"] = exc

    thread = threading.Thread(target=_worker, name="capture-render", daemon=True)
    thread.start()
    thread.join()

    if "value" in error:
        raise error["value"]
    return result["value"]


def _render_blocking(url: str) -> _Rendered:
    """Drive one headless-Chromium capture, owning the Playwright lifecycle.

    Called only from :func:`_run_on_dedicated_thread`, so the ``sync_playwright``
    instance it starts is confined to that one thread for its whole lifetime
    and torn down before the thread exits.

    Steps (Req 1.3, 2.7, 2.8, 3.1, 4.1, 4.3):

    1. Start Playwright and launch headless Chromium.
    2. Open a fresh 1280×900 context, isolating this capture.
    3. Install the ``page.route("**/*")`` SSRF guard on every request.
    4. Navigate, wait for ``networkidle`` (capped at 15 s), scroll once.
    5. Read the title, HTML, screenshot, main status, and final URL.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[*_CHROMIUM_STABILITY_ARGS, *_STEALTH_LAUNCH_ARGS],
        )
        logger.info("Launched headless Chromium for capture")
        # Fresh context per capture: no cookies, cache, or storage leak between
        # captures (design: "a fresh context per message"). Locale/timezone and
        # realistic headers make the (legitimate) visit look like a normal
        # browser rather than bare automation.
        context = browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers=_EXTRA_HTTP_HEADERS,
        )
        # Mask the most-checked automation tells before any page script runs.
        context.add_init_script(_STEALTH_INIT_SCRIPT)
        # Bound every Playwright operation (new_page, goto, title, screenshot)
        # so a hung renderer fails fast INSIDE the handler's CHECK_TIMEOUT_S
        # budget with a clear PlaywrightTimeoutError, rather than silently
        # stalling on Playwright's 30s default and risking the whole budget.
        context.set_default_timeout(NAVIGATION_TIMEOUT_MS)
        try:
            page = context.new_page()
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
            # SSRF guard stays on EVERY request (Req 1.3) — stealth never
            # relaxes the security route guard.
            page.route("**/*", _route_guard)

            response = page.goto(url, wait_until="commit")
            main_status = response.status if response is not None else None

            # Give lazily-loaded content a chance to settle, but never block the
            # whole budget on a page that keeps a socket open.
            try:
                page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                logger.info("networkidle not reached within cap; continuing", extra={"url": url})

            _scroll_once(page)

            html = page.content()
            title = page.title()
            screenshot = page.screenshot(type="png")
            final_url = page.url
        finally:
            context.close()
            browser.close()

    return _Rendered(
        html=html,
        title=title,
        screenshot=screenshot,
        main_status=main_status,
        final_url=final_url,
    )


def _scroll_once(page: Page) -> None:
    """Scroll to the bottom once to trigger lazy-loaded reviews (Req 3.1).

    Many review widgets load more content on first scroll. One scroll to the
    document's full height, followed by a short settle, is enough to surface
    them without an unbounded scroll loop.
    """
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1_000)
    except PlaywrightError:  # pragma: no cover - a page may block evaluation
        logger.debug("Scroll step failed; continuing with current content")
