"""Unit tests for the optional host-override registry (Task 8).

Covers :mod:`app.extraction.overrides` (Requirement 7):

- the registry **ships empty** — ``get_override`` returns ``None`` for any host
  until something is registered (Requirement 7.1);
- ``register`` / ``get_override`` round-trip, keyed by registrable domain so a
  bare host, a domain, or a URL all resolve to the same entry;
- an override whose output **passes the same validation as a Review Locator
  result** (post-processes into at least one Verified Review *and* its selectors
  validate) is accepted, with the Verified Reviews read from the page by code;
- an override whose output **fails** that validation (here: selectors that match
  far more than the verified reviews, so over-selection is too high) is rejected
  and produces no reviews, so the caller falls back to the AI path (Requirement
  7.2).

The "passes the same validation" gate mirrors exactly what ``build_plan`` does
to a Locator result: :func:`postprocess.postprocess` then
:func:`selectors.validate_selectors` with the configured ``selector_min_agreement``.

_Validates: Requirements 7.1, 7.2_
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.extraction import overrides
from app.extraction.cleaner import build_clean_result, resolve_ref
from app.extraction.models import (
    CleanedPage,
    LocatorItem,
    LocatorResult,
    LocatorSelectors,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

#: Five review cards, each a wrapper ``article.review-card`` whose body
#: paragraph holds the verified text.  The ``item`` selector points at the
#: wrapper (whose text contains the body text), matching how selector
#: validation works across the package.
_BODIES = [
    "Great tool, the setup took an afternoon but it worked well overall.",
    "Support was responsive and fixed my issue within a single day here.",
    "The dashboard is intuitive and the reports export cleanly to CSV too.",
    "Pricing is fair for a small team and the onboarding was painless now.",
    "Occasional slowness at peak hours but reliable the rest of the time ok.",
]

_REVIEW_PAGE = (
    "<html><body><main>"
    + "".join(
        f'<article class="review-card">'
        f'<p class="body">{body}</p>'
        f'<span class="author">Reviewer {i}</span>'
        f"</article>"
        # Extra non-review cards that a too-greedy selector would also match,
        # used by the failing-override case to push over-selection past 20%.
        for i, body in enumerate(_BODIES)
    )
    + '<div class="card">Unrelated promo block number one, buy now today.</div>'
    + '<div class="card">Unrelated promo block number two, buy now today.</div>'
    + '<div class="card">Unrelated promo block number three, buy now today.</div>'
    + "</main></body></html>"
)


def _cleaned(html: str) -> CleanedPage:
    """Build a real :class:`CleanedPage` so override refs resolve correctly."""
    result = build_clean_result(html)
    return CleanedPage(lines=result.lines, lookup=result.lookup, chunks=[result.lines])


def _ref_for_text(cleaned: CleanedPage, html: str, needle: str) -> str:
    """Return the ref of the smallest element whose text contains ``needle``.

    Shortest-match picks the leaf body element over its enclosing card, so each
    ref points at exactly the intended field (same approach as the postprocess
    tests).
    """
    best_ref: str | None = None
    best_len: int | None = None
    for ref in cleaned.lookup:
        node = resolve_ref(html, cleaned.lookup, ref)
        if node is None:
            continue
        text = node.text()
        if needle in text and (best_len is None or len(text) < best_len):
            best_ref, best_len = ref, len(text)
    if best_ref is None:
        raise AssertionError(f"no ref resolves to text containing {needle!r}")
    return best_ref


def _item_ref_for_body(cleaned: CleanedPage, html: str, body: str) -> str:
    """Return the ref of the ``article`` card wrapping ``body``.

    The item ref is the smallest element that contains both the body text and
    the author line — i.e. the card wrapper, not the body paragraph.
    """
    best_ref: str | None = None
    best_len: int | None = None
    for ref in cleaned.lookup:
        node = resolve_ref(html, cleaned.lookup, ref)
        if node is None:
            continue
        text = node.text()
        if body in text and "Reviewer" in text and (best_len is None or len(text) < best_len):
            best_ref, best_len = ref, len(text)
    if best_ref is None:
        raise AssertionError(f"no card ref wraps body {body!r}")
    return best_ref


def _passing_override(html: str, url: str, cleaned: CleanedPage) -> LocatorResult | None:
    """An override that correctly points at the five review cards.

    It references each card and its body text by reference ID (text is read from
    those elements by code) and suggests the tight ``article.review-card`` item
    selector, which reproduces exactly the five verified reviews — so it clears
    the same gate a Locator result must clear.
    """
    items = [
        LocatorItem(
            item_ref=_item_ref_for_body(cleaned, html, body),
            text_ref=_ref_for_text(cleaned, html, body),
            kind="review",
        )
        for body in _BODIES
    ]
    return LocatorResult(
        has_reviews=True,
        rating_scale=5,
        items=items,
        selectors=LocatorSelectors(item="article.review-card", text=".body"),
    )


def _over_selecting_override(html: str, url: str, cleaned: CleanedPage) -> LocatorResult | None:
    """An override that reads the right reviews but suggests a greedy selector.

    Post-processing still yields the five Verified Reviews (the items point at
    the real cards), but the suggested ``item`` selector ``.card, article`` also
    matches the three unrelated promo ``div.card`` blocks, pushing over-selection
    above the 20% limit — so selector validation fails and the override is
    rejected, exactly as a Locator result with the same selectors would be.
    """
    items = [
        LocatorItem(
            item_ref=_item_ref_for_body(cleaned, html, body),
            text_ref=_ref_for_text(cleaned, html, body),
            kind="review",
        )
        for body in _BODIES
    ]
    return LocatorResult(
        has_reviews=True,
        rating_scale=5,
        items=items,
        # Greedy: matches the 5 real cards AND the 3 promo .card divs → 3/8 extra
        # = 0.375 over-selection > 0.2, so validate_selectors rejects it.
        selectors=LocatorSelectors(item=".card, article.review-card"),
    )


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Keep the shipped-empty invariant: clear the registry around every test."""
    overrides.clear_registry()
    yield
    overrides.clear_registry()


# ---------------------------------------------------------------------------
# Registry: ships empty (Requirement 7.1)
# ---------------------------------------------------------------------------


class TestRegistryShipsEmpty:
    def test_get_override_is_none_by_default(self) -> None:
        assert overrides.get_override("https://www.example.com/reviews") is None
        assert overrides.get_override("example.com") is None
        assert overrides.get_override("anything.co.uk") is None

    def test_registered_hosts_empty_by_default(self) -> None:
        assert overrides.registered_hosts() == frozenset()

    def test_run_override_with_no_override_declines(self) -> None:
        cleaned = _cleaned(_REVIEW_PAGE)
        outcome = overrides.run_override(_REVIEW_PAGE, "https://www.example.com/reviews", cleaned)
        assert outcome.passed is False
        assert outcome.reviews == []
        assert outcome.result is None


# ---------------------------------------------------------------------------
# register / get_override round-trip (keyed by registrable domain)
# ---------------------------------------------------------------------------


class TestRegisterRoundTrip:
    def test_register_then_get_by_host_domain_and_url(self) -> None:
        overrides.register("www.example.com", _passing_override)

        # Same registrable domain resolves whether looked up by host, bare
        # domain, a different subdomain, or a full URL.
        assert overrides.get_override("www.example.com") is _passing_override
        assert overrides.get_override("example.com") is _passing_override
        assert overrides.get_override("shop.example.com") is _passing_override
        assert overrides.get_override("https://example.com/x/y?z=1") is _passing_override
        assert overrides.registered_hosts() == frozenset({"example.com"})

    def test_unregister_removes_entry(self) -> None:
        overrides.register("example.com", _passing_override)
        overrides.unregister("https://www.example.com/reviews")
        assert overrides.get_override("example.com") is None

    def test_last_registration_wins(self) -> None:
        overrides.register("example.com", _passing_override)
        overrides.register("example.com", _over_selecting_override)
        assert overrides.get_override("example.com") is _over_selecting_override

    def test_different_domains_are_independent(self) -> None:
        overrides.register("example.com", _passing_override)
        assert overrides.get_override("other.org") is None


# ---------------------------------------------------------------------------
# Passing override: same validation as a Locator result (Requirement 7.2)
# ---------------------------------------------------------------------------


class TestPassingOverride:
    def test_passing_override_is_accepted_with_verified_reviews(self) -> None:
        overrides.register("example.com", _passing_override)
        cleaned = _cleaned(_REVIEW_PAGE)

        outcome = overrides.run_override(_REVIEW_PAGE, "https://www.example.com/reviews", cleaned)

        assert outcome.passed is True
        assert len(outcome.reviews) == len(_BODIES)
        # Review text was read from the page by code, never supplied by the
        # override (the LocatorItem has no text field).
        assert {r.text for r in outcome.reviews} == set(_BODIES)

    def test_explicit_override_argument_is_used(self) -> None:
        """An explicitly passed override runs without needing registration."""
        cleaned = _cleaned(_REVIEW_PAGE)
        outcome = overrides.run_override(
            _REVIEW_PAGE,
            "https://www.example.com/reviews",
            cleaned,
            override=_passing_override,
        )
        assert outcome.passed is True
        assert len(outcome.reviews) == len(_BODIES)


# ---------------------------------------------------------------------------
# Failing override: rejected, caller falls back (Requirement 7.2)
# ---------------------------------------------------------------------------


class TestFailingOverride:
    def test_over_selecting_override_is_rejected(self) -> None:
        overrides.register("example.com", _over_selecting_override)
        cleaned = _cleaned(_REVIEW_PAGE)

        outcome = overrides.run_override(_REVIEW_PAGE, "https://www.example.com/reviews", cleaned)

        # Post-processing still read the real reviews, but the greedy selectors
        # fail validation, so the override's output is not used.
        assert outcome.passed is False
        assert outcome.reviews == []
        # The raw result is still carried for diagnostics.
        assert outcome.result is not None

    def test_declining_override_declines(self) -> None:
        """An override returning ``None`` yields a non-passing, empty result."""

        def _decline(html: str, url: str, cleaned: CleanedPage) -> LocatorResult | None:
            return None

        overrides.register("example.com", _decline)
        cleaned = _cleaned(_REVIEW_PAGE)
        outcome = overrides.run_override(_REVIEW_PAGE, "https://www.example.com/reviews", cleaned)
        assert outcome.passed is False
        assert outcome.reviews == []
        assert outcome.result is None

    def test_override_with_no_verified_reviews_is_rejected(self) -> None:
        """Refs that resolve to nothing yield no Verified Reviews → rejected."""

        def _bad_refs(html: str, url: str, cleaned: CleanedPage) -> LocatorResult | None:
            return LocatorResult(
                has_reviews=True,
                items=[LocatorItem(item_ref="e99999", kind="review")],
                selectors=LocatorSelectors(item="article.review-card"),
            )

        cleaned = _cleaned(_REVIEW_PAGE)
        outcome = overrides.run_override(
            _REVIEW_PAGE,
            "https://www.example.com/reviews",
            cleaned,
            override=_bad_refs,
        )
        assert outcome.passed is False
        assert outcome.reviews == []
