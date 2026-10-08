"""Property-based tests for app.ingestion.url_normalizer (dataset-ingestion 11).

Covers two Correctness Properties from the dataset-ingestion design:

- **Property 1: Normalization is idempotent.** *For any* URL,
  ``normalize(normalize(u))`` SHALL equal ``normalize(u)``.
  Validates: Requirement 6.1
- **Property 2: Cosmetic URL changes don't matter.** *For any* URL, adding
  tracking parameters, reordering query parameters, adding a fragment, toggling
  ``www.``, changing host case, or adding a trailing slash SHALL NOT change the
  normalized URL. Validates: Requirements 6.1, 6.2

Rather than feed arbitrary text (which rarely forms a valid http(s) URL), the
strategies build URLs from structured parts — a scheme, a host (optionally
``www.``-prefixed and with mixed case), a path, query parameters (content plus
tracking), and a fragment — so Hypothesis explores the space the normalizer
actually operates over. A fixed tracking-parameter list is passed to
``normalize`` so the property does not depend on configuration/env.
"""

from __future__ import annotations

from urllib.parse import urlencode

from app.ingestion.url_normalizer import normalize
from hypothesis import given
from hypothesis import strategies as st

# A fixed tracking list so the property is independent of config/env. Includes
# the common cases from the design (``utm_*``, ``gclid``, ``fbclid``, ``ref``).
_TRACKING = ["utm_source", "utm_medium", "utm_campaign", "gclid", "fbclid", "ref"]

# Query-parameter *keys* that are not tracking params (so they survive
# normalization). Kept disjoint from ``_TRACKING`` so a kept key is never
# accidentally dropped.
_CONTENT_KEYS = st.sampled_from(["q", "page", "sort", "id", "lang", "category", "tab"])
_CONTENT_VALUES = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122, categories=("Ll", "Lu", "Nd")),
    min_size=0,
    max_size=8,
)

# Host labels: lowercase ascii letters/digits so a reversed-case variant is a
# clean cosmetic change (and the host is a valid DNS-ish label).
_LABEL = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122),
    min_size=1,
    max_size=10,
)


@st.composite
def _hosts(draw: st.DrawFn) -> str:
    """A bare registrable host like ``example.com`` (no scheme, no ``www.``)."""
    label = draw(_LABEL)
    tld = draw(st.sampled_from(["com", "net", "org", "io", "co"]))
    return f"{label}.{tld}"


@st.composite
def _paths(draw: st.DrawFn) -> str:
    """A URL path with 0–3 segments, no trailing slash (added by variants)."""
    segments = draw(
        st.lists(
            st.text(
                alphabet=st.characters(min_codepoint=97, max_codepoint=122, categories=("Ll",)),
                min_size=1,
                max_size=8,
            ),
            min_size=0,
            max_size=3,
        )
    )
    return "/" + "/".join(segments) if segments else ""


@st.composite
def _content_params(draw: st.DrawFn) -> list[tuple[str, str]]:
    """A list of non-tracking (kept) query parameters, keys unique."""
    pairs = draw(
        st.lists(
            st.tuples(_CONTENT_KEYS, _CONTENT_VALUES),
            min_size=0,
            max_size=4,
        )
    )
    # De-duplicate keys so reordering is unambiguous (one value per key).
    seen: dict[str, str] = {}
    for key, value in pairs:
        seen.setdefault(key, value)
    return list(seen.items())


@st.composite
def _tracking_params(draw: st.DrawFn) -> list[tuple[str, str]]:
    """A list of tracking query parameters that normalization must drop."""
    return draw(
        st.lists(
            st.tuples(st.sampled_from(_TRACKING), _CONTENT_VALUES),
            min_size=0,
            max_size=4,
        )
    )


@st.composite
def _urls(draw: st.DrawFn) -> str:
    """Build a syntactically valid http(s) URL from structured parts."""
    scheme = draw(st.sampled_from(["http", "https"]))
    host = draw(_hosts())
    www = draw(st.booleans())
    path = draw(_paths())
    content = draw(_content_params())
    query = urlencode(content)
    fragment = draw(st.sampled_from(["", "section", "top"]))

    netloc = f"www.{host}" if www else host
    url = f"{scheme}://{netloc}{path}"
    if query:
        url += f"?{query}"
    if fragment:
        url += f"#{fragment}"
    return url


@given(url=_urls())
def test_normalization_is_idempotent(url: str) -> None:
    """Property 1: Normalization is idempotent.

    For any URL, normalize(normalize(u)) == normalize(u).
    Validates: Requirement 6.1
    """
    once = normalize(url, tracking_params=_TRACKING)
    twice = normalize(once, tracking_params=_TRACKING)
    assert twice == once


@given(data=st.data(), url=_urls())
def test_cosmetic_changes_do_not_matter(data: st.DataObject, url: str) -> None:
    """Property 2: Cosmetic URL changes don't matter.

    Adding tracking parameters, reordering query parameters, adding a fragment,
    toggling www., changing host case, or adding a trailing slash SHALL NOT
    change the normalized URL.
    Validates: Requirements 6.1, 6.2
    """
    baseline = normalize(url, tracking_params=_TRACKING)

    # Build a cosmetically-different variant of the same URL and assert it
    # normalizes to the same string. Each transformation below is "cosmetic".
    scheme, rest = url.split("://", 1)
    # Separate netloc+path from any query/fragment the generator added.
    netloc_path = rest.split("?", 1)[0].split("#", 1)[0]
    netloc, _, path = netloc_path.partition("/")
    path = f"/{path}" if path else ""

    # Recover the content params from the baseline (the kept, sorted ones).
    content = data.draw(_content_params())
    tracking = data.draw(_tracking_params())

    # Rebuild using the SAME content params as a fresh base so both sides share
    # identical kept query content; the point is the cosmetic deltas below.
    base_host = netloc[len("www.") :] if netloc.startswith("www.") else netloc
    base_query = urlencode(content)
    base_url = f"{scheme}://{base_host}{path}"
    if base_query:
        base_url += f"?{base_query}"
    expected = normalize(base_url, tracking_params=_TRACKING)

    # 1. toggle www.
    toggled_host = base_host if netloc.startswith("www.") else f"www.{base_host}"
    # 2. change host case
    cased_host = toggled_host.upper()
    # 3. add trailing slash to the path
    slashed_path = f"{path}/" if path else "/"
    # 4. reorder query params + append tracking params (dropped) + a fragment
    reordered = list(reversed(content)) + tracking
    variant_query = urlencode(reordered)

    variant = f"{scheme.upper()}://{cased_host}{slashed_path}"
    if variant_query:
        variant += f"?{variant_query}"
    variant += "#" + data.draw(st.sampled_from(["frag", "top", "x"]))

    assert normalize(variant, tracking_params=_TRACKING) == expected
    # Sanity: the baseline the generator produced normalizes to a stable form
    # too (idempotence overlap keeps the two properties consistent).
    assert normalize(baseline, tracking_params=_TRACKING) == baseline
