"""Integration tests for app.ingestion.url_validator.probe (task 1.4).

These exercise the probe over **real sockets** with real redirect responses,
proving Requirements 2.1, 2.2, 2.3, and 2.5 end to end:

* a direct ``200``;
* a ``301 → 200`` chain, with both hops recorded;
* a ``302 → 404`` chain reported as not reachable;
* a redirect loop detected;
* more than 10 redirects refused with "Too many redirects".

Redirect behaviour can't be produced by the static ``fixtures`` nginx
container, so these tests run a tiny in-process HTTP server bound to
``127.0.0.1`` and allow it through ``SSRF_TEST_ALLOW_HOSTS=localhost,127.0.0.1``
(the same mechanism the design uses for the fixtures host). A separate test
also fetches a real ``200`` page from the Compose ``fixtures`` container
(``localhost:9090``) when it is up, so the ``make test-int`` path is covered.

Run with: ``make test-int`` (``pytest -m integration``). The in-process-server
tests run anywhere; the fixtures-container test skips cleanly when the Compose
stack is not running.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator

import httpx
import pytest
from app.core.config import get_settings
from app.ingestion.url_validator import probe

pytestmark = pytest.mark.integration

_FIXTURES_BASE = "http://localhost:9090"


# ---------------------------------------------------------------------------
# In-process redirect server
# ---------------------------------------------------------------------------


class _ScenarioHandler(http.server.BaseHTTPRequestHandler):
    """Serve the redirect scenarios the probe needs to be exercised against."""

    def log_message(self, *args: object) -> None:  # noqa: D401 - silence stdout
        return

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        host = self.headers.get("Host", "")
        base = f"http://{host}"
        path = self.path

        if path == "/ok":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><title>OK</title></html>")
        elif path == "/moved":  # 301 -> /ok
            self.send_response(301)
            self.send_header("Location", f"{base}/ok")
            self.end_headers()
        elif path == "/gone":  # 302 -> /missing (404)
            self._redirect(f"{base}/missing")
        elif path == "/loop-a":
            self._redirect(f"{base}/loop-b")
        elif path == "/loop-b":
            self._redirect(f"{base}/loop-a")
        elif path.startswith("/chain/"):
            n = int(path.rsplit("/", 1)[1])
            self._redirect(f"{base}/chain/{n + 1}")
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def server_base() -> Iterator[str]:
    """Start the in-process redirect server; yield its base URL."""
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ScenarioHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _allow_localhost(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Allow 127.0.0.1 / localhost through the SSRF guard for these tests."""
    monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", "localhost,127.0.0.1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Redirect scenarios (in-process server)
# ---------------------------------------------------------------------------


def test_direct_200(server_base: str) -> None:
    result = probe(f"{server_base}/ok")
    assert result.ok is True
    assert result.status == 200
    assert len(result.hops) == 1


def test_301_to_200(server_base: str) -> None:
    result = probe(f"{server_base}/moved")
    assert result.ok is True
    assert result.final_url == f"{server_base}/ok"
    assert [h.status for h in result.hops] == [301, 200]
    # Every hop carries a timestamp (Requirement 2.5).
    assert all(h.timestamp for h in result.hops)


def test_302_to_404(server_base: str) -> None:
    result = probe(f"{server_base}/gone")
    assert result.ok is False
    assert result.status == 404
    assert [h.status for h in result.hops] == [302, 404]


def test_redirect_loop(server_base: str) -> None:
    result = probe(f"{server_base}/loop-a")
    assert result.ok is False
    assert result.reason == "Redirect loop detected"


def test_too_many_redirects(server_base: str) -> None:
    result = probe(f"{server_base}/chain/0", max_redirects=10)
    assert result.ok is False
    assert result.reason == "Too many redirects"
    assert len(result.hops) == 11


# ---------------------------------------------------------------------------
# Real fixtures container (Compose) — covers the make test-int wiring
# ---------------------------------------------------------------------------


def _fixtures_up() -> bool:
    try:
        resp = httpx.get(f"{_FIXTURES_BASE}/", timeout=2.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def test_fixtures_container_200() -> None:
    """Probe a real page served by the Compose ``fixtures`` nginx container."""
    if not _fixtures_up():
        pytest.skip("fixtures container not reachable; run under `make test-int`")
    result = probe(f"{_FIXTURES_BASE}/extraction/plain_list/")
    assert result.ok is True
    assert result.status == 200
    assert result.final_url.endswith("/extraction/plain_list/")
