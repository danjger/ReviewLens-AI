"""Unit tests for structured-data parsing (Task 3).

Covers reading JSON-LD and microdata ``Review`` objects nested at any depth
under the supported container types (``Product``, ``Organization``,
``LocalBusiness``, ``SoftwareApplication``), reading ``AggregateRating`` review
counts, verifying reviews against the page's visible text, and the edge cases
(no structured data, malformed JSON-LD), implemented in
``app/extraction/structured.py``.

A property test for Property 1 ("No invented text") over generated structured
pages lives in ``tests/property/`` and is written by a later task; these are
focused example-based tests of each behavior.

_Validates: Requirements 3.1, 3.2_
"""

from __future__ import annotations

import json

from app.extraction import parse_structured
from app.extraction.models import StructuredResult


def _jsonld_html(payload: dict[str, object] | list[object], body: str = "") -> str:
    """Wrap a JSON-LD payload (and optional visible body) in an HTML document."""
    script = json.dumps(payload)
    return (
        "<html><body>"
        f'<script type="application/ld+json">{script}</script>'
        f"<main>{body}</main>"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# JSON-LD: containers and nesting (Requirement 3.1)
# ---------------------------------------------------------------------------


class TestJsonLdContainers:
    """Reviews nested under each supported container type are read."""

    def _assert_single_container(self, container_type: str) -> None:
        body_text = "Great tool, the setup took an afternoon but worked well."
        payload = {
            "@context": "https://schema.org",
            "@type": container_type,
            "name": "Acme Thing",
            "review": [
                {
                    "@type": "Review",
                    "reviewBody": body_text,
                    "reviewRating": {"@type": "Rating", "ratingValue": "5"},
                    "author": {"@type": "Person", "name": "Dana"},
                    "datePublished": "2023-01-02",
                    "name": "Loved it",
                }
            ],
        }
        html = _jsonld_html(payload, body=f"<p>{body_text}</p>")
        result = parse_structured(html, body_text)
        assert len(result.reviews) == 1
        review = result.reviews[0]
        assert review.text == body_text
        assert review.rating == 5.0
        assert review.author == "Dana"
        assert review.date == "2023-01-02"
        assert review.title == "Loved it"
        assert review.source_ref == "structured"

    def test_product_container(self) -> None:
        self._assert_single_container("Product")

    def test_organization_container(self) -> None:
        self._assert_single_container("Organization")

    def test_local_business_container(self) -> None:
        self._assert_single_container("LocalBusiness")

    def test_software_application_container(self) -> None:
        self._assert_single_container("SoftwareApplication")

    def test_full_iri_type_is_matched(self) -> None:
        body_text = "The dashboard is responsive and the exports are reliable enough."
        payload = {
            "@type": "https://schema.org/Product",
            "name": "Acme CRM",
            "review": {
                "@type": "https://schema.org/Review",
                "reviewBody": body_text,
            },
        }
        html = _jsonld_html(payload, body=body_text)
        result = parse_structured(html, body_text)
        assert len(result.reviews) == 1
        assert result.reviews[0].text == body_text

    def test_review_nested_at_depth(self) -> None:
        """A Review buried below the ``review`` property is still found."""
        body_text = "Nested deeply but definitely a genuine customer review here."
        payload = {
            "@type": "Product",
            "name": "Deep Product",
            "review": {
                "@type": "ItemList",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "item": {
                            "@type": "Review",
                            "reviewBody": body_text,
                        },
                    }
                ],
            },
        }
        html = _jsonld_html(payload, body=body_text)
        result = parse_structured(html, body_text)
        assert len(result.reviews) == 1
        assert result.reviews[0].text == body_text

    def test_graph_wrapper(self) -> None:
        """Objects under a top-level ``@graph`` list are walked."""
        body_text = "Solid product overall, the onboarding emails were a nice touch."
        payload = {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "WebPage", "name": "Reviews"},
                {
                    "@type": "Product",
                    "name": "Graph Product",
                    "review": [{"@type": "Review", "reviewBody": body_text}],
                },
            ],
        }
        html = _jsonld_html(payload, body=body_text)
        result = parse_structured(html, body_text)
        assert len(result.reviews) == 1
        assert result.reviews[0].text == body_text


# ---------------------------------------------------------------------------
# AggregateRating counts (Requirement 3.1)
# ---------------------------------------------------------------------------


class TestAggregateRating:
    """``AggregateRating`` review counts are read, preferring ``reviewCount``."""

    def test_review_count_read(self) -> None:
        body_text = "This counts as a verified review with enough visible text."
        payload = {
            "@type": "Product",
            "name": "Counted",
            "aggregateRating": {
                "@type": "AggregateRating",
                "reviewCount": "1540",
                "ratingValue": "4.5",
            },
            "review": [{"@type": "Review", "reviewBody": body_text}],
        }
        html = _jsonld_html(payload, body=body_text)
        result = parse_structured(html, body_text)
        assert result.review_count == 1540

    def test_rating_count_fallback(self) -> None:
        """``ratingCount`` is used only when ``reviewCount`` is absent."""
        payload = {
            "@type": "Product",
            "name": "Fallback",
            "aggregateRating": {
                "@type": "AggregateRating",
                "ratingCount": "87",
            },
        }
        html = _jsonld_html(payload)
        result = parse_structured(html, "")
        assert result.review_count == 87

    def test_review_count_preferred_over_rating_count(self) -> None:
        payload = {
            "@type": "Product",
            "name": "Prefer",
            "aggregateRating": {
                "@type": "AggregateRating",
                "reviewCount": "200",
                "ratingCount": "999",
            },
        }
        html = _jsonld_html(payload)
        result = parse_structured(html, "")
        assert result.review_count == 200

    def test_no_aggregate_rating_leaves_count_none(self) -> None:
        body_text = "A review with no aggregate rating object anywhere on the page."
        payload = {
            "@type": "Product",
            "name": "NoAgg",
            "review": [{"@type": "Review", "reviewBody": body_text}],
        }
        html = _jsonld_html(payload, body=body_text)
        result = parse_structured(html, body_text)
        assert result.review_count is None


# ---------------------------------------------------------------------------
# Microdata (Requirement 3.1)
# ---------------------------------------------------------------------------


class TestMicrodata:
    """Microdata Review objects (itemscope/itemtype/itemprop) are read."""

    def test_microdata_review_and_count(self) -> None:
        body_text = "Microdata review body that appears verbatim in the page text."
        html = (
            "<html><body>"
            '<div itemscope itemtype="https://schema.org/Product">'
            '<span itemprop="name">Widget</span>'
            '<div itemprop="review" itemscope itemtype="https://schema.org/Review">'
            f'<span itemprop="reviewBody">{body_text}</span>'
            '<span itemprop="author">Pat</span>'
            '<meta itemprop="datePublished" content="2022-05-01" />'
            '<div itemprop="reviewRating" itemscope '
            'itemtype="https://schema.org/Rating">'
            '<span itemprop="ratingValue">4</span></div>'
            "</div>"
            '<div itemprop="aggregateRating" itemscope '
            'itemtype="https://schema.org/AggregateRating">'
            '<span itemprop="reviewCount">42</span></div>'
            "</div>"
            "</body></html>"
        )
        result = parse_structured(html, body_text)
        assert len(result.reviews) == 1
        review = result.reviews[0]
        assert review.text == body_text
        assert review.author == "Pat"
        assert review.rating == 4.0
        assert review.date == "2022-05-01"
        assert result.review_count == 42


# ---------------------------------------------------------------------------
# Verification against visible text (Requirement 3.2)
# ---------------------------------------------------------------------------


class TestVerification:
    """A structured review is kept only if its text is on the page."""

    def test_unverifiable_review_dropped(self) -> None:
        body_text = "This review body is NOT present in the rendered visible text."
        payload = {
            "@type": "Product",
            "name": "Phantom",
            "review": [{"@type": "Review", "reviewBody": body_text}],
        }
        # Visible text deliberately omits the structured body.
        html = _jsonld_html(payload, body="Unrelated page content about pricing.")
        result = parse_structured(html, "Unrelated page content about pricing.")
        assert result.reviews == []

    def test_verified_kept_unverified_dropped_together(self) -> None:
        present = "The battery life easily lasts a full working day for me."
        absent = "A fabricated quote that never appears on the actual page."
        payload = {
            "@type": "Product",
            "name": "Mixed",
            "review": [
                {"@type": "Review", "reviewBody": present},
                {"@type": "Review", "reviewBody": absent},
            ],
        }
        html = _jsonld_html(payload, body=present)
        result = parse_structured(html, present)
        assert len(result.reviews) == 1
        assert result.reviews[0].text == present

    def test_verification_ignores_whitespace_and_unicode(self) -> None:
        """NFKC + whitespace collapse let equivalent text verify."""
        structured_body = "Great   value\nfor\tmoney, would buy again."
        # Visible text uses a non-breaking space and different spacing; NFKC and
        # whitespace collapse should still match.
        visible = "Great value\u00a0for money, would buy again."
        payload = {
            "@type": "Product",
            "name": "Norm",
            "review": [{"@type": "Review", "reviewBody": structured_body}],
        }
        html = _jsonld_html(payload, body=visible)
        result = parse_structured(html, visible)
        assert len(result.reviews) == 1
        assert result.reviews[0].text == structured_body


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """No structured data and malformed JSON-LD are handled gracefully."""

    def test_no_structured_data_empty_result(self) -> None:
        html = "<html><body><p>Just a plain page with no structured data.</p></body></html>"
        result = parse_structured(html, "Just a plain page with no structured data.")
        assert result == StructuredResult()
        assert result.reviews == []
        assert result.review_count is None

    def test_malformed_json_ld_handled(self) -> None:
        body_text = "A valid plain paragraph of visible page content here."
        html = (
            "<html><body>"
            '<script type="application/ld+json">{ this is not valid json ]</script>'
            f"<p>{body_text}</p>"
            "</body></html>"
        )
        # extruct skips the broken block; no reviews, no crash.
        result = parse_structured(html, body_text)
        assert result.reviews == []
        assert result.review_count is None

    def test_review_without_body_skipped(self) -> None:
        payload = {
            "@type": "Product",
            "name": "NoBody",
            "review": [{"@type": "Review", "author": {"@type": "Person", "name": "X"}}],
        }
        html = _jsonld_html(payload, body="Some visible content on the page.")
        result = parse_structured(html, "Some visible content on the page.")
        assert result.reviews == []

    def test_duplicate_bodies_deduplicated(self) -> None:
        body_text = "An identical review body repeated twice in the structured data."
        payload = {
            "@type": "Product",
            "name": "Dupe",
            "review": [
                {"@type": "Review", "reviewBody": body_text},
                {"@type": "Review", "reviewBody": body_text},
            ],
        }
        html = _jsonld_html(payload, body=body_text)
        result = parse_structured(html, body_text)
        assert len(result.reviews) == 1

    def test_top_level_review_without_container(self) -> None:
        body_text = "A standalone review object sitting at the document top level."
        payload = {"@type": "Review", "reviewBody": body_text}
        html = _jsonld_html(payload, body=body_text)
        result = parse_structured(html, body_text)
        assert len(result.reviews) == 1
        assert result.reviews[0].text == body_text

    def test_author_as_plain_string(self) -> None:
        body_text = "The author field here is a plain string rather than an object."
        payload = {
            "@type": "Product",
            "name": "StrAuthor",
            "review": [{"@type": "Review", "reviewBody": body_text, "author": "Jamie"}],
        }
        html = _jsonld_html(payload, body=body_text)
        result = parse_structured(html, body_text)
        assert len(result.reviews) == 1
        assert result.reviews[0].author == "Jamie"
