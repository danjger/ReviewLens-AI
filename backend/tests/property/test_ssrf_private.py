"""Property-based test for app.ingestion.url_validator.assert_public_host (11).

- **Property 3: Private addresses are always refused.** *For any* IPv4 or IPv6
  address in a private, loopback, link-local, or cloud-metadata range,
  ``assert_public_host`` SHALL refuse it, including when reached through a
  redirect. Validates: Requirement 1.3

The unit tests (``tests/unit/ingestion/test_assert_public_host.py``) pin a
handful of representative literals; this property exhausts the ranges with
Hypothesis. Addresses are generated two ways to cover both code paths in
``assert_public_host``:

1. As a bare IP **literal** in the URL host, which resolves to itself without
   DNS; the refusal must hold directly.
2. Behind a **hostname** whose DNS resolution (``socket.getaddrinfo``) is
   stubbed to return the generated private address — the redirect re-check path,
   since ``probe`` calls ``assert_public_host`` afresh for every hop with the
   hop's URL. Both the first hop and a redirect target go through the exact same
   call, so a logical property on ``assert_public_host`` with a stubbed resolver
   is the redirect guarantee.

The allowlist (``SSRF_TEST_ALLOW_HOSTS``) is cleared so no generated host is
bypassed; the property is specifically about *refusal* of private ranges.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator

import pytest
from app.core.config import get_settings
from app.ingestion.url_validator import SsrfError, assert_public_host
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


@pytest.fixture(autouse=True)
def _clear_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the SSRF allowlist and settings cache so nothing is bypassed."""
    monkeypatch.delenv("SSRF_TEST_ALLOW_HOSTS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Strategies: addresses in each disallowed range
# ---------------------------------------------------------------------------


def _ipv4_in(network: str) -> st.SearchStrategy[str]:
    """Any address inside the given IPv4 ``network`` (CIDR), as a string."""
    net = ipaddress.ip_network(network)
    lo = int(net.network_address)
    hi = int(net.broadcast_address)
    return st.integers(min_value=lo, max_value=hi).map(lambda n: str(ipaddress.IPv4Address(n)))


# Private, loopback, link-local (incl. the metadata address 169.254.169.254),
# unspecified — every IPv4 range assert_public_host must refuse.
_PRIVATE_IPV4 = st.one_of(
    _ipv4_in("10.0.0.0/8"),
    _ipv4_in("172.16.0.0/12"),
    _ipv4_in("192.168.0.0/16"),
    _ipv4_in("127.0.0.0/8"),  # loopback
    _ipv4_in("169.254.0.0/16"),  # link-local, includes 169.254.169.254 metadata
    st.just("0.0.0.0"),  # unspecified
    st.just("169.254.169.254"),  # cloud metadata, explicitly
)


def _ipv6_in(network: str) -> st.SearchStrategy[str]:
    """Any address inside the given IPv6 ``network`` (CIDR), compressed form."""
    net = ipaddress.ip_network(network)
    lo = int(net.network_address)
    # Cap the span so the integer strategy stays cheap for huge IPv6 blocks.
    hi = min(int(net.broadcast_address), lo + 2**32)
    return st.integers(min_value=lo, max_value=hi).map(lambda n: str(ipaddress.IPv6Address(n)))


# IPv6 loopback (::1), link-local (fe80::/10), unique-local/private (fc00::/7),
# plus IPv4-mapped private addresses which must be judged by the embedded IPv4.
_PRIVATE_IPV6 = st.one_of(
    st.just("::1"),  # loopback
    _ipv6_in("fe80::/112"),  # link-local (narrow span)
    _ipv6_in("fc00::/112"),  # unique-local (private)
    _PRIVATE_IPV4.map(lambda v4: f"::ffff:{v4}"),  # IPv4-mapped private
)

_PRIVATE_ANY = st.one_of(_PRIVATE_IPV4, _PRIVATE_IPV6)


def _url_for(ip: str) -> str:
    """Wrap an IP literal in a URL, bracketing IPv6 as the URL syntax requires."""
    addr = ipaddress.ip_address(ip)
    host = f"[{ip}]" if addr.version == 6 else ip
    return f"http://{host}/path"


# ---------------------------------------------------------------------------
# Property 3 — direct literal (the first hop, and a redirect to an IP literal)
# ---------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ip=_PRIVATE_ANY)
def test_private_ip_literal_is_refused(ip: str) -> None:
    """Property 3: Private addresses are always refused.

    For any private/loopback/link-local/metadata IP (v4 or v6) given as a URL
    host literal, assert_public_host refuses it.
    Validates: Requirement 1.3
    """
    with pytest.raises(SsrfError):
        assert_public_host(_url_for(ip))


# ---------------------------------------------------------------------------
# Property 3 — through a hostname that resolves to a private address, modelling
# the per-hop re-check a redirect to an internal host triggers.
# ---------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ip=_PRIVATE_ANY)
def test_hostname_resolving_to_private_is_refused_after_redirect(
    ip: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Property 3: Private addresses are always refused, including via redirect.

    probe() re-runs assert_public_host for every hop with that hop's URL, so a
    redirect to an internal host is just another assert_public_host call. With
    DNS stubbed to the generated private address, the (redirect) host is
    refused.
    Validates: Requirement 1.3
    """
    addr = ipaddress.ip_address(ip)
    family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        return [(family, socket.SOCK_STREAM, 6, "", (str(addr), 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    # The redirect target host; its resolution (stubbed) is private → refused.
    with pytest.raises(SsrfError):
        assert_public_host("http://redirect-target.example.test/internal")
