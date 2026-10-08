"""Unit tests for app.ingestion.robots.check.

Covers Requirement 3.12: a disallow is surfaced as a warning (never changing
the verdict), and any failure to fetch robots.txt is treated as "no
restriction". Uses an httpx MockTransport so no network is needed; a public IP
literal host keeps the per-hop SSRF check happy.
"""

from __future__ import annotations

import httpx
from app.ingestion.robots import check
from app.ingestion.url_validator import BROWSER_HEADERS


def _client(handler: object) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        follow_redirects=True,
        headers=BROWSER_HEADERS,
    )


def test_disallowed_page_warns() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/robots.txt"
        return httpx.Response(200, text="User-agent: *\nDisallow: /reviews")

    result = check("http://8.8.8.8/reviews/acme", client=_client(handler))
    assert result.allowed is False
    assert result.warning is not None


def test_allowed_page_no_warning() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\nDisallow: /admin")

    result = check("http://8.8.8.8/reviews/acme", client=_client(handler))
    assert result.allowed is True
    assert result.warning is None


def test_missing_robots_treated_as_allowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    result = check("http://8.8.8.8/reviews", client=_client(handler))
    assert result.allowed is True


def test_fetch_error_treated_as_allowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    result = check("http://8.8.8.8/reviews", client=_client(handler))
    assert result.allowed is True


def test_empty_robots_allows_everything() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="")

    result = check("http://8.8.8.8/anything", client=_client(handler))
    assert result.allowed is True


def test_ssrf_blocked_host_treated_as_allowed() -> None:
    # 127.0.0.1 is refused by assert_public_host → no fetch, no restriction.
    result = check("http://127.0.0.1/reviews")
    assert result.allowed is True
