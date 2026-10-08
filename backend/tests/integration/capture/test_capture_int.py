"""Integration tests for app.capture.engine.render (dataset-ingestion task 2.2).

These drive the real headless-browser Capture module against fixture pages
served over HTTP by the Compose ``fixtures`` nginx container, writing the
rendered HTML and screenshot to the LocalStack S3 bucket. They prove the
Capture requirements end to end:

* **normal page** — HTML and screenshot stored in S3, title read, main status
  200, final URL unchanged (Req 4.1, 4.3);
* **blocked sub-request** — a page whose only sub-resource is an image on
  ``127.0.0.1`` renders, but the capture SSRF route guard aborts that
  sub-request (Req 1.3);
* **meta-refresh redirect** — the browser ends on the redirect target, so
  :func:`render` reports ``redirected=True`` and a ``final_url`` on the target
  (Req 2.8);
* **403 main response** — a directory with no index returns 403, surfaced as
  ``main_status == 403`` (Req 2.7);
* **lazy-loaded reviews** — reviews injected only on scroll are present in the
  captured HTML after the single post-render scroll (Req 3.1 / 4.1).

Environment
-----------
The tests reach the fixtures pages at ``http://localhost:9090/capture/...``.
``localhost`` is allowed through the capture SSRF guard
(``SSRF_TEST_ALLOW_HOSTS=localhost``) while ``127.0.0.1`` is deliberately *not*,
which is exactly what makes the blocked sub-request observable. S3 writes go to
the LocalStack bucket (``reviewlens-local`` at ``http://localhost:4566``), the
same services ``make up`` provides.

The module skips cleanly when Playwright/Chromium is not installed, when the
fixtures container is not reachable, or when LocalStack S3 is not reachable, so
the unit suite stays green in a bare environment. Run with ``make test-int``.

Each test writes under its own ``checks/{uuid}/{uuid}/`` prefix and deletes it
afterward (testing convention: tests clean up their own S3 prefix).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from app.core.config import get_settings
from app.storage import s3

pytestmark = pytest.mark.integration

_FIXTURES_BASE = "http://localhost:9090"
_S3_BUCKET = "reviewlens-local"
_S3_ENDPOINT = "http://localhost:4566"


# ---------------------------------------------------------------------------
# Availability probes (skip cleanly when the stack / browser is absent)
# ---------------------------------------------------------------------------


def _fixtures_up() -> bool:
    try:
        resp = httpx.get(f"{_FIXTURES_BASE}/capture/normal/", timeout=2.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _s3_up() -> bool:
    try:
        resp = httpx.get(f"{_S3_ENDPOINT}/_localstack/health", timeout=2.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _chromium_available() -> bool:
    """True when Playwright can launch headless Chromium in this environment."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # noqa: BLE001 - any launch failure means "skip"
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _require_stack() -> None:
    """Skip the whole module unless the browser, fixtures, and S3 are present."""
    if not _chromium_available():
        pytest.skip("Playwright/Chromium not available; run under the workers image")
    if not _fixtures_up():
        pytest.skip("fixtures container not reachable; run under `make test-int`")
    if not _s3_up():
        pytest.skip("LocalStack S3 not reachable; run under `make test-int`")


@pytest.fixture(autouse=True)
def _capture_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point settings at LocalStack S3 and allow ``localhost`` through the guard.

    ``localhost`` is allowed so the browser can load the fixtures pages, but
    ``127.0.0.1`` is intentionally left out of the allowlist so the capture
    route guard still blocks the loopback image sub-request.
    """
    monkeypatch.setenv("S3_BUCKET", _S3_BUCKET)
    monkeypatch.setenv("AWS_ENDPOINT_URL", _S3_ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", "localhost")
    get_settings.cache_clear()
    s3.reset_client()
    try:
        yield
    finally:
        get_settings.cache_clear()
        s3.reset_client()


@pytest.fixture()
def prefix() -> Iterator[str]:
    """A unique check prefix; delete every object under it after the test."""
    check_id = uuid.uuid4().hex
    item_id = uuid.uuid4().hex
    key_prefix = f"checks/{check_id}/{item_id}/"
    try:
        yield key_prefix
    finally:
        client = s3._get_s3_client()
        listed = client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=key_prefix)
        contents = listed.get("Contents", [])
        for obj in contents:
            key = obj.get("Key")
            if key:
                client.delete_object(Bucket=_S3_BUCKET, Key=key)


@pytest.fixture(autouse=True)
def _shutdown_browser() -> Iterator[None]:
    """Close the per-process browser after each test to isolate cases."""
    from app.capture import engine

    try:
        yield
    finally:
        engine.shutdown_browser()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_s3(key: str) -> bytes:
    """Return the bytes stored at *key* in the LocalStack bucket."""
    client = s3._get_s3_client()
    response = client.get_object(Bucket=_S3_BUCKET, Key=key)
    return response["Body"].read()


def _s3_key_exists(key: str) -> bool:
    client = s3._get_s3_client()
    try:
        client.head_object(Bucket=_S3_BUCKET, Key=key)
        return True
    except client.exceptions.ClientError:
        return False


# ---------------------------------------------------------------------------
# 1. Normal page
# ---------------------------------------------------------------------------


def test_normal_page_renders_and_stores(prefix: str) -> None:
    """A normal page: HTML + screenshot in S3, title read, 200, final URL kept.

    Validates: Requirements 4.1, 4.3.
    """
    from app.capture.engine import render

    url = f"{_FIXTURES_BASE}/capture/normal/"
    result = render(url, prefix)

    assert result.main_status == 200
    assert result.final_url == url
    assert result.redirected is False
    assert "Northwind Standing Desk" in result.page_title

    # HTML and screenshot were stored under the given prefix.
    assert result.html_key == f"{prefix}page.html"
    assert result.snapshot_key == f"{prefix}snapshot.png"
    assert _s3_key_exists(result.html_key)
    assert _s3_key_exists(result.snapshot_key)

    html = _read_s3(result.html_key).decode("utf-8")
    # The rendered HTML contains the review text copied from the page.
    assert "The motor is quiet" in html

    # The screenshot is a PNG (magic header).
    screenshot = _read_s3(result.snapshot_key)
    assert screenshot[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# 2. Blocked loopback sub-request
# ---------------------------------------------------------------------------


def test_loopback_image_is_blocked_but_page_renders(prefix: str) -> None:
    """An image on 127.0.0.1 is aborted by the route guard; the page still renders.

    Validates: Requirement 1.3.
    """
    from app.capture.engine import render

    url = f"{_FIXTURES_BASE}/capture/localhost_image/"
    result = render(url, prefix)

    # The document itself (on allowed ``localhost``) renders fine.
    assert result.main_status == 200
    assert "Harbor Point Hotel" in result.page_title

    html = _read_s3(result.html_key).decode("utf-8")
    # The page body rendered — its reviews are present — even though the
    # loopback image sub-request was aborted by the SSRF route guard.
    assert "Spotless rooms" in html
    assert 'src="http://127.0.0.1' in html  # the <img> tag is still in the DOM


# ---------------------------------------------------------------------------
# 3. Meta-refresh (client-side) redirect
# ---------------------------------------------------------------------------


def test_meta_refresh_redirect_is_recorded(prefix: str) -> None:
    """The browser ends on the meta-refresh target; render reports it (Req 2.8).

    Validates: Requirement 2.8.
    """
    from app.capture.engine import render

    url = f"{_FIXTURES_BASE}/capture/meta_refresh/"
    result = render(url, prefix)

    assert result.redirected is True
    assert result.final_url != url
    assert result.final_url.rstrip("/").endswith("/capture/normal")
    # The captured page is the target, so its title and content are the normal page.
    assert "Northwind Standing Desk" in result.page_title


# ---------------------------------------------------------------------------
# 4. 403 main response
# ---------------------------------------------------------------------------


def test_forbidden_main_status(prefix: str) -> None:
    """A directory with no index returns 403, surfaced as main_status (Req 2.7).

    Validates: Requirement 2.7.
    """
    from app.capture.engine import render

    url = f"{_FIXTURES_BASE}/capture/forbidden/"
    result = render(url, prefix)

    assert result.main_status == 403


# ---------------------------------------------------------------------------
# 5. Lazy-loaded reviews surface after the single scroll
# ---------------------------------------------------------------------------


def test_lazy_reviews_surface_after_scroll(prefix: str) -> None:
    """Reviews injected only on scroll are in the captured HTML (Req 3.1 / 4.1).

    Validates: Requirements 4.1 (and 3.1 — the single post-render scroll).
    """
    from app.capture.engine import render

    url = f"{_FIXTURES_BASE}/capture/lazy/"
    result = render(url, prefix)

    assert result.main_status == 200
    html = _read_s3(result.html_key).decode("utf-8")
    # These review texts exist in the DOM only after the lazy loader ran on scroll.
    assert "Grippy on wet rock" in html
    assert "Great traction on technical trails" in html
