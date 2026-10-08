"""Property-based test for next-page resolution (Task 6).

Covers Property 7 from ``.kiro/specs/review-extraction/design.md``:

- **Property 7: Same-site pagination.** *For any* page and plan, a returned
  next-page URL SHALL be on the same registrable domain as the page URL
  (Requirement 5.3).

The strategy builds a page whose URL has a random registrable domain and host,
seeds it with a mix of same-domain and off-domain next links (via ``rel=next``,
"Next" labels, and page-number query/path forms, both relative and absolute),
and sometimes also supplies a plan rule and a Locator next-page ref pointing at
one of those links.  Whatever :func:`app.extraction.next_page` returns, if it
returns a URL at all, that URL must share the page URL's registrable domain.

``next_page`` is pure and AI-free; the suite-wide autouse ``FakeClaude`` fixture
keeps any incidental token counting offline.
"""

from __future__ import annotations

from datetime import UTC, datetime

import tldextract
from app.extraction.models import (
    ExtractionPlan,
    LocatorNextPage,
    LocatorResult,
    NextPageRule,
)
from app.extraction.pagination import next_page
from hypothesis import given
from hypothesis import strategies as st

# Registrable-domain extractor configured offline (bundled snapshot, no network),
# matching the one pagination uses, so the oracle agrees with the code.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())


def _registrable_domain(url: str) -> str:
    extracted = _EXTRACT(url)
    domain = getattr(extracted, "top_domain_under_public_suffix", None)
    if domain is None:
        domain = getattr(extracted, "registered_domain", "")
    return domain or ""


# A short DNS label (letters/digits), used to build hosts and registrable names.
_label = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd")),
    min_size=1,
    max_size=8,
).filter(lambda s: s[0].isalpha())

# A small set of public suffixes present in the bundled snapshot.
_suffix = st.sampled_from(["com", "org", "net", "co.uk", "io"])


@st.composite
def _page_and_links(
    draw: st.DrawFn,
) -> tuple[str, str, ExtractionPlan | None, LocatorResult | None]:
    """Draw ``(html, page_url, plan, locator)`` with mixed same/off-domain links.

    The page URL gets a random registrable domain and (sometimes) a subdomain.
    Same-domain next candidates may live on the bare host or a sibling subdomain;
    off-domain candidates live on a different registrable domain entirely.  Links
    are emitted via several of the recognised forms so the generic patterns, the
    plan rule, and the Locator ref are all exercised across the space.
    """
    reg_label = draw(_label)
    suffix = draw(_suffix)
    registrable = f"{reg_label}.{suffix}"

    page_sub = draw(st.sampled_from(["", "www.", "reviews.", "shop."]))
    page_host = f"{page_sub}{registrable}"
    page_num = draw(st.integers(min_value=1, max_value=5))
    page_url = f"https://{page_host}/catalog/item?page={page_num}"

    # A different registrable domain for off-site links.
    other_label = draw(_label)
    other_suffix = draw(_suffix)
    off_registrable = f"{other_label}.{other_suffix}"
    # Guard against the two randomly colliding into the same registrable domain.
    if off_registrable == registrable:
        off_registrable = f"x{other_label}.{other_suffix}"

    next_num = page_num + 1
    anchors: list[str] = []

    # Same-domain candidates (one or more of these forms), relative + absolute.
    same_sub = draw(st.sampled_from(["", "www.", "shop.", "cdn."]))
    same_host = f"{same_sub}{registrable}"
    if draw(st.booleans()):
        anchors.append(f'<a rel="next" href="/catalog/item?page={next_num}">Next</a>')
    if draw(st.booleans()):
        anchors.append(f'<a href="https://{same_host}/catalog/item?page={next_num}">Next</a>')
    if draw(st.booleans()):
        anchors.append(f'<a href="/catalog/item/page/{next_num}">more</a>')

    # Off-domain candidates (should never be returned).
    if draw(st.booleans()):
        anchors.append(
            f'<a rel="next" href="https://{off_registrable}/catalog?page={next_num}">Next</a>'
        )
    if draw(st.booleans()):
        anchors.append(f'<a href="https://{off_registrable}/catalog?page={next_num}">\u203a</a>')

    # Sometimes a script-driven control with no URL.
    if draw(st.booleans()):
        anchors.append('<button class="load-more">Load more</button>')

    draw(st.randoms()).shuffle(anchors)
    html = f"<html><body><main>{''.join(anchors)}</main></body></html>"

    # Optionally attach a plan rule: a url_template (same or off domain) or none.
    plan: ExtractionPlan | None = None
    rule_choice = draw(st.sampled_from(["none", "same_template", "off_template", "no_plan"]))
    if rule_choice == "same_template":
        rule = NextPageRule(
            type="url_template", template=f"https://{same_host}/catalog/item?page={{page}}"
        )
        plan = _make_plan(rule)
    elif rule_choice == "off_template":
        rule = NextPageRule(
            type="url_template", template=f"https://{off_registrable}/catalog?page={{page}}"
        )
        plan = _make_plan(rule)
    elif rule_choice == "none":
        plan = _make_plan(NextPageRule(type="none"))

    # Optionally attach a Locator ref pointing at the first anchor (if any).
    locator: LocatorResult | None = None
    if anchors and draw(st.booleans()):
        locator = LocatorResult(next_page=LocatorNextPage(ref="e2"))

    return html, page_url, plan, locator


def _make_plan(rule: NextPageRule) -> ExtractionPlan:
    return ExtractionPlan(
        created_at=datetime(2024, 1, 1, tzinfo=UTC).isoformat(),
        method="selectors",
        next_page_rule=rule,
    )


@given(case=_page_and_links())
def test_returned_next_url_is_same_registrable_domain(
    case: tuple[str, str, ExtractionPlan | None, LocatorResult | None],
) -> None:
    """Property 7: Same-site pagination.

    For any page and plan, a returned next-page URL SHALL be on the same
    registrable domain as the page URL.
    Validates: Requirement 5.3
    """
    html, page_url, plan, locator = case

    result = next_page(html, page_url, plan, locator=locator)

    if result.url is None:
        # No URL is always acceptable; a reason must then be given.
        assert result.reason_if_none is not None
        return

    page_domain = _registrable_domain(page_url)
    returned_domain = _registrable_domain(result.url)
    assert returned_domain == page_domain
    assert returned_domain != ""
