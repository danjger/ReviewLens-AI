"""Unit tests for app.ingestion.url_validator.assert_public_host.

Covers Requirement 1.3 / design Property 3: private, loopback, link-local, and
cloud-metadata addresses (IPv4 and IPv6) are refused, DNS names that resolve to
such addresses are refused, the check can be re-run after a redirect, and the
test-only allowlist bypasses resolution.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest
from app.core.config import get_settings
from app.ingestion.url_validator import SsrfError, assert_public_host


@pytest.fixture(autouse=True)
def _clear_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("SSRF_TEST_ALLOW_HOSTS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# IP literals: refused ranges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "http://127.0.0.1/",  # loopback
        "http://10.0.0.5/",  # private
        "http://192.168.1.1/",  # private
        "http://172.16.0.1/",  # private
        "http://169.254.169.254/",  # cloud metadata (link-local)
        "http://169.254.1.1/",  # link-local
        "http://0.0.0.0/",  # unspecified
    ],
)
def test_private_ipv4_refused(ip: str) -> None:
    with pytest.raises(SsrfError):
        assert_public_host(ip)


@pytest.mark.parametrize(
    "ip",
    [
        "http://[::1]/",  # IPv6 loopback
        "http://[fe80::1]/",  # IPv6 link-local
        "http://[fc00::1]/",  # IPv6 unique-local (private)
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
        "http://[::ffff:169.254.169.254]/",  # IPv4-mapped metadata
    ],
)
def test_private_ipv6_refused(ip: str) -> None:
    with pytest.raises(SsrfError):
        assert_public_host(ip)


# ---------------------------------------------------------------------------
# Public addresses pass
# ---------------------------------------------------------------------------


def test_public_ipv4_allowed() -> None:
    # Documentation/public literal; no resolution needed for an IP literal.
    assert_public_host("https://8.8.8.8/")


def test_public_ipv6_allowed() -> None:
    assert_public_host("https://[2001:4860:4860::8888]/")


# ---------------------------------------------------------------------------
# DNS resolution
# ---------------------------------------------------------------------------


def test_hostname_resolving_to_private_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SsrfError):
        assert_public_host("http://evil.example.test/")


def test_hostname_with_any_private_address_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # One public, one private → still refused (DNS rebinding defence).
    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SsrfError):
        assert_public_host("http://mixed.example.test/")


def test_hostname_resolving_to_public_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert_public_host("http://good.example.test/")


def test_unresolvable_host_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SsrfError):
        assert_public_host("http://nope.example.test/")


def test_missing_host_refused() -> None:
    with pytest.raises(SsrfError):
        assert_public_host("not-a-url")


# ---------------------------------------------------------------------------
# Re-check after a redirect
# ---------------------------------------------------------------------------


def test_recheck_after_redirect_to_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public first hop passes; a redirect to a private host is refused."""

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        if host == "public.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    # First hop OK.
    assert_public_host("http://public.example.test/")
    # Redirect target refused when re-checked.
    with pytest.raises(SsrfError):
        assert_public_host("http://internal.example.test/")


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def test_allowlisted_host_bypasses_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", "fixtures,localhost")
    get_settings.cache_clear()
    # Would otherwise be refused as loopback, but it's allowlisted by name.
    assert_public_host("http://localhost:9090/page")
    assert_public_host("http://fixtures/page")


def test_non_allowlisted_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", "fixtures")
    get_settings.cache_clear()
    with pytest.raises(SsrfError):
        assert_public_host("http://127.0.0.1/")
