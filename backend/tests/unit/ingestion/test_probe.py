"""Unit tests for app.ingestion.url_validator.probe.

Covers Requirements 2.1, 2.2, 2.3, 2.5, and 2.6 using an httpx MockTransport so
no network or Compose stack is needed. The matching against-the-fixtures
integration tests live in ``tests/integration/ingestion/test_probe_int.py``.

Public IP literals (``8.8.8.8``) are used as hosts so the per-hop SSRF check
passes without DNS, while still exercising the manual-redirect machinery.
"""

from __future__ import annotations

import httpx
import pytest
from app.ingestion.url_validator import (
    BROWSER_HEADERS,
    ProbeError,
    SsrfError,
    probe,
)


def _client(handler: object) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        follow_redirects=False,
        headers=BROWSER_HEADERS,
    )


def test_direct_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    result = probe("http://8.8.8.8/", client=_client(handler))
    assert result.ok is True
    assert result.status == 200
    assert result.final_url == "http://8.8.8.8/"
    assert len(result.hops) == 1
    assert result.hops[0].status == 200
    assert result.hops[0].timestamp  # ISO timestamp recorded


def test_301_then_200_records_hops() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "http://8.8.8.8/new"})
        return httpx.Response(200, text="ok")

    result = probe("http://8.8.8.8/old", client=_client(handler))
    assert result.ok is True
    assert result.final_url == "http://8.8.8.8/new"
    assert [h.status for h in result.hops] == [301, 200]
    assert [h.url for h in result.hops] == [
        "http://8.8.8.8/old",
        "http://8.8.8.8/new",
    ]


def test_302_to_404_not_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "http://8.8.8.8/missing"})
        return httpx.Response(404)

    result = probe("http://8.8.8.8/a", client=_client(handler))
    assert result.ok is False
    assert result.status == 404
    assert result.reason is not None and "404" in result.reason
    assert [h.status for h in result.hops] == [302, 404]


def test_relative_redirect_resolved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return httpx.Response(200)

    result = probe("http://8.8.8.8/a", client=_client(handler))
    assert result.ok is True
    assert result.final_url == "http://8.8.8.8/b"


def test_redirect_loop_detected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "http://8.8.8.8/b"})
        return httpx.Response(302, headers={"location": "http://8.8.8.8/a"})

    result = probe("http://8.8.8.8/a", client=_client(handler))
    assert result.ok is False
    assert result.reason == "Redirect loop detected"


def test_too_many_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Every URL redirects onward to a new, never-repeating path.
        n = int(request.url.path.strip("/") or "0")
        return httpx.Response(302, headers={"location": f"http://8.8.8.8/{n + 1}"})

    result = probe("http://8.8.8.8/0", client=_client(handler), max_redirects=10)
    assert result.ok is False
    assert result.reason == "Too many redirects"
    # 11 requests made (initial + 10 redirects) before giving up.
    assert len(result.hops) == 11


def test_browser_headers_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200)

    probe("http://8.8.8.8/", client=_client(handler))
    assert "Chrome" in seen["user-agent"]
    assert seen["accept-language"].startswith("en-US")
    assert "text/html" in seen["accept"]


def test_redirect_to_private_host_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/internal"})

    with pytest.raises(SsrfError):
        probe("http://8.8.8.8/", client=_client(handler))


def test_timeout_raises_probe_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow")

    with pytest.raises(ProbeError) as exc:
        probe("http://8.8.8.8/", client=_client(handler))
    assert exc.value.code == "TIMEOUT"


def test_redirect_without_location() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301)  # no Location header

    result = probe("http://8.8.8.8/", client=_client(handler))
    assert result.ok is False
    assert result.reason == "Redirect without a target"
