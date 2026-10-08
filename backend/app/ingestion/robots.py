"""robots.txt evaluation for the viability warning (Requirement 3.12).

``check(final_url)`` fetches the site's ``robots.txt`` (SSRF-checked, with a
short timeout) and evaluates whether the ``*`` user agent is disallowed from
the page. The result is surfaced as a *warning* on the verdict: a disallow is
noted so an analyst can see it, but it never changes the verdict on its own.

A failure to fetch ``robots.txt`` (network error, timeout, non-200, or a host
that the SSRF guard refuses) is treated as "no restriction" — the absence of a
robots file means nothing is disallowed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.ingestion.url_validator import BROWSER_HEADERS, SsrfError, assert_public_host

logger = logging.getLogger(__name__)

#: Short timeout for fetching robots.txt — it must never slow the check.
_ROBOTS_TIMEOUT_S = 3.0

#: The user agent robots rules are evaluated against (general crawlers).
_USER_AGENT = "*"


@dataclass(frozen=True)
class RobotsResult:
    """Outcome of the robots.txt check for one page.

    Attributes:
        allowed: True when general crawlers may fetch the page (the default
            when robots.txt is missing or unreadable).
        warning: A plain-language warning when the page is disallowed, else
            ``None``. This is attached to the verdict but never changes it.
    """

    allowed: bool
    warning: str | None = None


def _robots_url(final_url: str) -> str:
    """Return the ``robots.txt`` URL for *final_url*'s origin."""
    parts = urlsplit(final_url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def check(final_url: str, *, client: httpx.Client | None = None) -> RobotsResult:
    """Return whether general crawlers may fetch *final_url* per robots.txt.

    Any failure to fetch or parse robots.txt is treated as "no restriction"
    (Requirement 3.12), so this function never raises for a transport or SSRF
    error — it returns ``allowed=True`` instead.

    Args:
        final_url: The page URL that returned HTTP 200.
        client: Optional pre-built ``httpx.Client`` (used by tests).

    Returns:
        A :class:`RobotsResult`.
    """
    robots_url = _robots_url(final_url)

    try:
        assert_public_host(robots_url)
    except SsrfError:
        # Can't safely fetch it; treat as unrestricted.
        logger.info("robots.txt host not fetchable; treating as no restriction")
        return RobotsResult(allowed=True)

    owns_client = client is None
    if client is None:
        client = httpx.Client(
            follow_redirects=True,
            timeout=_ROBOTS_TIMEOUT_S,
            headers=BROWSER_HEADERS,
        )

    try:
        try:
            response = client.request("GET", robots_url)
        except httpx.HTTPError:
            return RobotsResult(allowed=True)

        if response.status_code != 200:
            return RobotsResult(allowed=True)

        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        allowed = parser.can_fetch(_USER_AGENT, final_url)
    finally:
        if owns_client:
            client.close()

    if allowed:
        return RobotsResult(allowed=True)
    return RobotsResult(
        allowed=False,
        warning="This page is disallowed for general crawlers by the site's robots.txt",
    )
