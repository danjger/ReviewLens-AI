"""Unit tests for app.capture.engine.

These cover the browser-free decision logic:

* the ``page.route`` SSRF guard aborts a request to a disallowed host and
  continues a request to a public host (Requirement 1.3 / 2.7);
* ``render`` refuses a non-public main URL before launching the browser,
  raising ``CaptureBlockedError`` (Requirement 1.3);
* ``CaptureResult`` records a client-side redirect via ``redirected`` /
  ``final_url`` (Requirement 2.8).

Full rendering of fixture pages (lazy content, 403 main response, blocked
sub-request) is covered by the integration tests in task 2.2.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.capture import engine as render_mod
from app.capture.engine import (
    CaptureBlockedError,
    CaptureResult,
    _route_guard,
)
from app.core.config import get_settings
from app.ingestion.url_validator import SsrfError


@pytest.fixture(autouse=True)
def _clear_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("SSRF_TEST_ALLOW_HOSTS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeRoute:
    """Records whether the route was aborted or continued."""

    def __init__(self) -> None:
        self.aborted_with: str | None = None
        self.continued = False

    def abort(self, error_code: str | None = None) -> None:
        self.aborted_with = error_code or "aborted"

    def continue_(self) -> None:
        self.continued = True


class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


# ---------------------------------------------------------------------------
# Route guard (Requirement 1.3 / 2.7)
# ---------------------------------------------------------------------------


class TestRouteGuard:
    def test_blocked_host_is_aborted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(url: str) -> None:
            raise SsrfError("Address not allowed")

        monkeypatch.setattr(render_mod, "assert_public_host", _raise)
        route = _FakeRoute()
        _route_guard(route, _FakeRequest("http://127.0.0.1/evil.png"))  # type: ignore[arg-type]

        assert route.aborted_with == "blockedbyclient"
        assert route.continued is False

    def test_public_host_is_continued(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(render_mod, "assert_public_host", lambda url: None)
        route = _FakeRoute()
        _route_guard(route, _FakeRequest("https://example.com/app.js"))  # type: ignore[arg-type]

        assert route.continued is True
        assert route.aborted_with is None

    def test_real_loopback_sub_request_is_aborted(self) -> None:
        """A real loopback URL is blocked without stubbing assert_public_host."""
        route = _FakeRoute()
        _route_guard(route, _FakeRequest("http://127.0.0.1/pixel.gif"))  # type: ignore[arg-type]

        assert route.aborted_with == "blockedbyclient"
        assert route.continued is False


# ---------------------------------------------------------------------------
# render pre-navigation SSRF refusal (Requirement 1.3)
# ---------------------------------------------------------------------------


class TestRenderMainGuard:
    def test_non_public_main_url_refused_before_browser(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-public target raises before any browser is launched."""

        def _should_not_launch() -> None:
            raise AssertionError("browser must not launch for a blocked main URL")

        monkeypatch.setattr(render_mod, "_get_browser", _should_not_launch)

        with pytest.raises(CaptureBlockedError):
            render_mod.render("http://169.254.169.254/latest/meta-data/", "checks/c1/u1/")


# ---------------------------------------------------------------------------
# CaptureResult (Requirement 2.8)
# ---------------------------------------------------------------------------


class TestCaptureResult:
    def test_redirected_flag(self) -> None:
        result = CaptureResult(
            final_url="https://example.com/final",
            main_status=200,
            page_title="Example",
            html_key="checks/c1/u1/page.html",
            snapshot_key="checks/c1/u1/snapshot.png",
            redirected=True,
        )
        assert result.redirected is True
        assert result.final_url == "https://example.com/final"
        assert result.main_status == 200
