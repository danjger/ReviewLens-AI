"""URL normalization for duplicate matching (Requirement 6.1).

``normalize(url)`` turns a URL into a canonical form so that two URLs that
differ only cosmetically map to the same string. This canonical form is what
the ingestion module stores in ``datasets.normalized_url`` /
``normalized_final_url`` and matches against when deciding whether a URL is
already tracked.

The canonical form applies these transformations:

1. lowercase the scheme and host;
2. remove a leading ``www.`` from the host;
3. drop the fragment (``#...``);
4. remove known tracking query parameters (``utm_*``, ``gclid``, ``fbclid``,
   ``ref``, and the rest of the configurable list in
   :attr:`app.core.config.Settings.tracking_params`);
5. sort the remaining query parameters;
6. remove a trailing slash from the path.

The tracking-parameter list is read from configuration, never hard-coded here,
so operators can extend it without a code change.

Correctness properties (see the design):

- **Property 1 — idempotent:** ``normalize(normalize(u)) == normalize(u)``.
- **Property 2 — cosmetic changes don't matter:** adding tracking parameters,
  reordering the query, adding a fragment, toggling ``www.``, changing host
  case, or adding a trailing slash does not change the result.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.config import get_settings


def _strip_default_port(host: str, scheme: str) -> str:
    """Remove the port when it is the default for the scheme.

    ``http`` on 80 and ``https`` on 443 are the defaults; stripping them keeps
    ``http://example.com`` and ``http://example.com:80`` equal.
    """
    if ":" not in host:
        return host
    # IPv6 literals are wrapped in brackets (``[::1]:80``); only treat the
    # segment after the final ``]`` or the single ``:`` as a port.
    hostpart, _, portpart = host.rpartition(":")
    if not portpart.isdigit():
        return host
    default = {"http": "80", "https": "443"}.get(scheme)
    if default is not None and portpart == default:
        return hostpart
    return host


def normalize(url: str, *, tracking_params: list[str] | None = None) -> str:
    """Return the canonical form of *url* for duplicate matching.

    Args:
        url: The URL to normalize. Leading/trailing whitespace is stripped.
        tracking_params: Optional override for the tracking-parameter list.
            When ``None`` (the default) the list comes from
            :func:`app.core.config.get_settings`. Matching is case-insensitive.

    Returns:
        The normalized URL string. Idempotent: normalizing an already
        normalized URL returns it unchanged.
    """
    if tracking_params is None:
        tracking_params = get_settings().tracking_params
    tracking = {p.lower() for p in tracking_params}

    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    host = _strip_default_port(parts.hostname or "", scheme)
    if host.startswith("www."):
        host = host[len("www.") :]

    # Reassemble the authority, preserving a non-default port (host already had
    # any default port stripped). userinfo is intentionally dropped: it does not
    # participate in duplicate identity for public review pages.
    netloc = host
    if parts.port is not None and _strip_default_port(f"{host}:{parts.port}", scheme) != host:
        netloc = f"{host}:{parts.port}"

    # Drop tracking parameters, keep the rest, then sort for a stable order.
    # ``keep_blank_values`` so ``?a=&b=1`` round-trips predictably.
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in tracking
    ]
    query = urlencode(sorted(kept))

    # Remove a trailing slash, including a bare root slash, so
    # ``example.com/`` and ``example.com`` collapse to the same form.
    path = parts.path.rstrip("/")

    # Fragment is dropped entirely (empty string).
    return urlunsplit((scheme, netloc, path, query, ""))
