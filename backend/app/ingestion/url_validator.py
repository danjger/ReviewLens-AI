"""URL input parsing, SSRF protection, and the reachability probe.

This module covers three concerns of the Check step:

* **Input parsing** (:func:`parse_submission`) — Requirements 1.1, 1.2, 1.4.
  Splits a submission into up to ``MAX_URLS_PER_CHECK`` lines, marks lines that
  are not well-formed ``http``/``https`` URLs as ``invalid``, normalizes the
  valid ones, and collapses duplicates within the batch to ``duplicate_in_batch``.

* **SSRF protection** (:func:`assert_public_host`) — Requirement 1.3. Resolves
  a URL's host and refuses any address in a private, loopback, link-local, or
  cloud-metadata range (IPv4 and IPv6). It is designed to be re-called after
  every redirect hop. A test-only allowlist (``SSRF_TEST_ALLOW_HOSTS``) lets
  integration tests reach the local fixtures host.

* **Reachability probe** (:func:`probe`) — Requirements 2.1, 2.2, 2.3, 2.5,
  2.6. Follows redirects *manually* so every hop is SSRF-checked and logged,
  sends browser-like headers, enforces a hop limit, and detects redirect loops.

Everything here is synchronous ``httpx`` so the same code runs in the Lambda
check handler and the container poller without an event loop requirement.

Design Property 3 (private addresses are always refused, including through a
redirect) is validated here and exhaustively in ``tests/property`` (task 11).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings
from app.ingestion.url_normalizer import normalize

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default maximum number of redirect hops to follow (Requirement 2.1).
MAX_REDIRECTS = 10

#: A current desktop Chrome request header set, so sites that block
#: non-browser clients judge the probe the same way the headless browser
#: will see it (Requirement 2.6).
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

#: The IPv4 cloud metadata endpoint (AWS/GCP/Azure link-local). It falls inside
#: the link-local range already, but is called out explicitly for clarity.
_METADATA_IPV4 = ipaddress.ip_address("169.254.169.254")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SsrfError(Exception):
    """Raised when a URL's host resolves to a disallowed (non-public) address.

    The check handler turns this into a ``wont_work`` verdict with the reason
    "Address not allowed" (see the design's Error Handling table).
    """


class ProbeError(Exception):
    """Raised when the reachability probe cannot complete.

    Carries a short machine-readable ``code`` (UPPER_SNAKE_CASE) so the caller
    can map it to a verdict reason without string matching.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Input parsing (Requirements 1.1, 1.2, 1.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedItem:
    """One line of a URL submission after parsing and normalization.

    ``state`` mirrors the ``POST /ingest/checks`` item states in the design:
    ``pending`` (a valid, first-seen URL to be checked), ``invalid`` (not a
    well-formed http/https URL), or ``duplicate_in_batch`` (a valid URL whose
    normalized form already appeared earlier in the same submission).
    """

    item_id: str
    input: str
    state: str  # "pending" | "invalid" | "duplicate_in_batch"
    normalized: str | None = None
    message: str | None = None


def _is_wellformed_http_url(url: str) -> bool:
    """Return True when *url* is a syntactically valid http/https URL.

    Requires an ``http``/``https`` scheme and a non-empty host. This is a
    syntax check only; reachability and SSRF are handled later.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    # ``hostname`` lower-cases and strips brackets; empty means no authority.
    return bool(parts.hostname)


def parse_submission(
    raw: str, *, max_urls: int | None = None, tracking_params: list[str] | None = None
) -> list[ParsedItem]:
    """Parse a multi-line URL submission into per-line items.

    Behaviour (Requirements 1.1, 1.2, 1.4):

    * At most ``max_urls`` non-empty lines are considered; extra lines are
      ignored. ``max_urls`` defaults to ``MAX_URLS_PER_CHECK`` from config.
    * Blank lines are skipped entirely (they produce no item).
    * A line that is not a well-formed ``http``/``https`` URL becomes an
      ``invalid`` item with a message; other lines are still processed.
    * Valid lines are normalized; the first occurrence of a normalized form is
      ``pending`` and any later line with the same normalized form is
      ``duplicate_in_batch``.

    Item IDs are stable positional labels (``u1``, ``u2``, …) over the
    considered lines, matching the design's example item IDs.
    """
    if max_urls is None:
        max_urls = get_settings().max_urls_per_check

    items: list[ParsedItem] = []
    seen_normalized: dict[str, str] = {}
    considered = 0

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if considered >= max_urls:
            break
        considered += 1
        item_id = f"u{considered}"

        if not _is_wellformed_http_url(stripped):
            items.append(
                ParsedItem(
                    item_id=item_id,
                    input=stripped,
                    state="invalid",
                    message="Not a valid http or https URL",
                )
            )
            continue

        normalized = normalize(stripped, tracking_params=tracking_params)
        if normalized in seen_normalized:
            items.append(
                ParsedItem(
                    item_id=item_id,
                    input=stripped,
                    state="duplicate_in_batch",
                    normalized=normalized,
                    message=f"Duplicate of {seen_normalized[normalized]} in this submission",
                )
            )
            continue

        seen_normalized[normalized] = item_id
        items.append(
            ParsedItem(
                item_id=item_id,
                input=stripped,
                state="pending",
                normalized=normalized,
            )
        )

    return items


# ---------------------------------------------------------------------------
# SSRF protection (Requirement 1.3)
# ---------------------------------------------------------------------------


def _address_is_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True when *ip* is in a range that must never be fetched.

    Covers private, loopback, link-local (which includes the 169.254.169.254
    cloud-metadata address), reserved, unspecified, and multicast ranges, for
    both IPv4 and IPv6. IPv6 is unwrapped first so that an IPv4-mapped address
    (``::ffff:127.0.0.1``) is judged by its embedded IPv4 address.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour

    if ip == _METADATA_IPV4:
        return True

    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def _resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve *host* to every IP address it maps to.

    A bare IP literal resolves to itself without a DNS lookup. A hostname is
    resolved through ``getaddrinfo`` so that *all* A/AAAA records are checked —
    a host that returns one public and one private address is still refused.
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfError(f"Could not resolve host {host!r}") from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        addresses.append(ipaddress.ip_address(sockaddr[0]))
    if not addresses:
        raise SsrfError(f"Host {host!r} resolved to no addresses")
    return addresses


def assert_public_host(url: str) -> None:
    """Raise :class:`SsrfError` unless *url*'s host is a public address.

    Resolves the host and refuses the request when **any** resolved address is
    private, loopback, link-local, or a cloud-metadata address (IPv4 or IPv6).
    Must be re-called for every redirect target so a redirect cannot smuggle a
    request to an internal address (design Property 3).

    Hosts listed in ``SSRF_TEST_ALLOW_HOSTS`` are allowed without resolution.
    That setting is forbidden in production by the config layer, so this
    allowlist only ever applies in development and test environments.

    Args:
        url: The absolute URL whose host will be fetched next.

    Raises:
        SsrfError: when the host is missing, unresolvable, or resolves to a
            disallowed address.
    """
    host = urlsplit(url).hostname
    if not host:
        raise SsrfError(f"URL has no host: {url!r}")

    if host in set(get_settings().ssrf_allowed_hosts):
        return

    for ip in _resolve_host(host):
        if _address_is_disallowed(ip):
            logger.warning(
                "SSRF blocked",
                extra={"blocked_host": host, "resolved_ip": str(ip)},
            )
            raise SsrfError(f"Address not allowed for host {host!r}")


# ---------------------------------------------------------------------------
# Reachability probe (Requirements 2.1, 2.2, 2.3, 2.5, 2.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hop:
    """One hop of the redirect chain (Requirement 2.5)."""

    url: str
    status: int
    timestamp: str  # ISO-8601 UTC


@dataclass
class ProbeResult:
    """The outcome of probing a Target URL.

    ``ok`` is True only when the final response status is 200 (Requirement
    2.2). ``hops`` records every response in order, including the final one.
    ``reason`` carries a plain-language explanation when ``ok`` is False.
    """

    final_url: str
    status: int
    ok: bool
    hops: list[Hop] = field(default_factory=list)
    reason: str | None = None


def probe(
    url: str,
    *,
    max_redirects: int = MAX_REDIRECTS,
    timeout_s: float = 15.0,
    client: httpx.Client | None = None,
) -> ProbeResult:
    """Follow redirects manually and report whether *url* ends in HTTP 200.

    Every hop (including the first request and each redirect target) is
    SSRF-checked with :func:`assert_public_host` before the request is made, so
    a redirect to a private address is refused (Requirement 1.3). Browser-like
    headers are sent (Requirement 2.6). The chain is capped at ``max_redirects``
    and a repeated URL is treated as a loop (Requirement 2.3).

    Args:
        url: The Target URL to probe.
        max_redirects: Maximum redirect hops before giving up (default 10).
        timeout_s: Per-request timeout in seconds.
        client: Optional pre-built ``httpx.Client`` (used by tests). When
            ``None`` a client is created and closed internally.

    Returns:
        A :class:`ProbeResult`. ``ok`` is True only for a final 200.

    Raises:
        SsrfError: when any hop's host is disallowed. The caller maps this to a
            ``wont_work`` verdict with reason "Address not allowed".
    """
    owns_client = client is None
    if client is None:
        # ``follow_redirects=False``: we follow them ourselves so each target
        # is SSRF-checked and logged.
        client = httpx.Client(
            follow_redirects=False,
            timeout=timeout_s,
            headers=BROWSER_HEADERS,
        )

    hops: list[Hop] = []
    visited: set[str] = set()
    current = url
    try:
        for _ in range(max_redirects + 1):
            assert_public_host(current)

            if current in visited:
                return ProbeResult(
                    final_url=current,
                    status=0,
                    ok=False,
                    hops=hops,
                    reason="Redirect loop detected",
                )
            visited.add(current)

            try:
                response = client.request("GET", current)
            except httpx.TimeoutException as exc:
                raise ProbeError("Page took too long to load", code="TIMEOUT") from exc
            except httpx.HTTPError as exc:
                raise ProbeError(f"Could not reach the page: {exc}", code="UNREACHABLE") from exc

            hops.append(
                Hop(
                    url=current,
                    status=response.status_code,
                    timestamp=datetime.now(UTC).isoformat(),
                )
            )

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return ProbeResult(
                        final_url=current,
                        status=response.status_code,
                        ok=False,
                        hops=hops,
                        reason="Redirect without a target",
                    )
                # Resolve relative redirects against the current URL.
                current = str(httpx.URL(current).join(location))
                continue

            ok = response.status_code == 200
            return ProbeResult(
                final_url=current,
                status=response.status_code,
                ok=ok,
                hops=hops,
                reason=None if ok else f"Status {response.status_code}",
            )

        # Fell out of the loop without a terminal response: too many hops.
        return ProbeResult(
            final_url=current,
            status=0,
            ok=False,
            hops=hops,
            reason="Too many redirects",
        )
    finally:
        if owns_client:
            client.close()
