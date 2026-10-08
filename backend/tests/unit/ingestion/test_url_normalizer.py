"""Unit tests for app.ingestion.url_normalizer.

Covers Requirement 6.1 transformations individually and in combination:
case (scheme + host), leading ``www.``, fragment removal, tracking-parameter
removal (configurable list), query-parameter sorting, and trailing-slash
removal. Also exercises idempotency (design Property 1) and cosmetic
invariance (design Property 2) on concrete examples; the exhaustive
property-based versions live in ``tests/property`` (task 11).
"""

from __future__ import annotations

import pytest
from app.ingestion.url_normalizer import normalize

# A fixed tracking-param list so these tests don't depend on configuration.
_TRACKING = [
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "msclkid",
    "ref",
    "source",
]


def _n(url: str) -> str:
    return normalize(url, tracking_params=_TRACKING)


class TestCase:
    def test_scheme_lowercased(self) -> None:
        assert _n("HTTP://example.com/p") == "http://example.com/p"

    def test_host_lowercased(self) -> None:
        assert _n("https://Example.COM/p") == "https://example.com/p"

    def test_path_case_preserved(self) -> None:
        # Paths are case-sensitive on most servers; must not be lowercased.
        assert _n("https://example.com/Product/ABC") == "https://example.com/Product/ABC"


class TestWww:
    def test_leading_www_removed(self) -> None:
        assert _n("https://www.example.com/p") == "https://example.com/p"

    def test_only_leading_www_removed(self) -> None:
        # A ``www.`` that is not the leading label stays.
        assert _n("https://www.sub.www.example.com/p") == "https://sub.www.example.com/p"

    def test_non_www_prefix_kept(self) -> None:
        assert _n("https://wwwx.example.com/p") == "https://wwwx.example.com/p"


class TestFragment:
    def test_fragment_removed(self) -> None:
        assert _n("https://example.com/p#reviews") == "https://example.com/p"

    def test_empty_fragment_removed(self) -> None:
        assert _n("https://example.com/p#") == "https://example.com/p"


class TestTrackingParams:
    def test_utm_params_removed(self) -> None:
        assert (
            _n("https://example.com/p?utm_source=x&utm_medium=y&a=1") == "https://example.com/p?a=1"
        )

    def test_gclid_fbclid_ref_removed(self) -> None:
        assert _n("https://example.com/p?gclid=1&fbclid=2&ref=3") == "https://example.com/p"

    def test_tracking_match_case_insensitive(self) -> None:
        assert _n("https://example.com/p?UTM_Source=x&a=1") == "https://example.com/p?a=1"

    def test_non_tracking_kept(self) -> None:
        assert _n("https://example.com/p?page=2") == "https://example.com/p?page=2"

    def test_configurable_list(self) -> None:
        # A param only removed when the caller lists it.
        assert normalize("https://example.com/p?sid=9", tracking_params=["sid"]) == (
            "https://example.com/p"
        )
        assert normalize("https://example.com/p?sid=9", tracking_params=[]) == (
            "https://example.com/p?sid=9"
        )


class TestQueryOrder:
    def test_params_sorted(self) -> None:
        assert _n("https://example.com/p?b=2&a=1&c=3") == "https://example.com/p?a=1&b=2&c=3"

    def test_reordering_is_invariant(self) -> None:
        assert _n("https://example.com/p?a=1&b=2") == _n("https://example.com/p?b=2&a=1")

    def test_repeated_keys_preserved_and_sorted(self) -> None:
        assert _n("https://example.com/p?a=2&a=1") == "https://example.com/p?a=1&a=2"


class TestTrailingSlash:
    def test_trailing_slash_removed(self) -> None:
        assert _n("https://example.com/p/") == "https://example.com/p"

    def test_root_slash_preserved(self) -> None:
        # A bare host keeps its single root slash (don't produce an empty path).
        assert _n("https://example.com/") == "https://example.com"

    def test_trailing_slash_before_query(self) -> None:
        assert _n("https://example.com/p/?a=1") == "https://example.com/p?a=1"


class TestDefaultPort:
    def test_default_http_port_stripped(self) -> None:
        assert _n("http://example.com:80/p") == "http://example.com/p"

    def test_default_https_port_stripped(self) -> None:
        assert _n("https://example.com:443/p") == "https://example.com/p"

    def test_non_default_port_kept(self) -> None:
        assert _n("https://example.com:8443/p") == "https://example.com:8443/p"


class TestCombined:
    def test_all_transformations_together(self) -> None:
        messy = "HTTPS://WWW.Example.COM/Product/?utm_source=nl&b=2&a=1#reviews"
        assert _n(messy) == "https://example.com/Product?a=1&b=2"


class TestIdempotency:
    """Design Property 1: normalize(normalize(u)) == normalize(u)."""

    @pytest.mark.parametrize(
        "url",
        [
            "HTTPS://WWW.Example.COM/Product/?utm_source=nl&b=2&a=1#reviews",
            "http://example.com:80/p/",
            "https://example.com/",
            "https://example.com/p?gclid=1",
        ],
    )
    def test_idempotent(self, url: str) -> None:
        once = _n(url)
        assert _n(once) == once


class TestCosmeticInvariance:
    """Design Property 2: cosmetic changes do not change the result."""

    def test_variants_equal(self) -> None:
        base = _n("https://example.com/p?a=1")
        assert _n("https://www.example.com/p?a=1") == base
        assert _n("https://EXAMPLE.com/p?a=1") == base
        assert _n("https://example.com/p/?a=1") == base
        assert _n("https://example.com/p?a=1#frag") == base
        assert _n("https://example.com/p?a=1&utm_source=x") == base
        assert _n("https://example.com/p?utm_medium=y&a=1") == base
