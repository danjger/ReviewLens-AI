"""Unit tests for next-page resolution (``app.extraction.pagination``), Task 6.

Covers :func:`app.extraction.next_page` and the reusable URL-template inference
helper, exercising each rule in Requirement 5 one at a time:

- the plan's **selector** rule → URL (Req 5.1 step 1);
- the plan's **url_template** rule → page *n + 1* URL (Req 5.1 step 1, 5.2);
- generic ``rel="next"`` (Req 5.1 step 2a);
- generic label ``Next`` / ``›`` / ``»`` (Req 5.1 step 2b);
- generic ``?page=`` / ``&p=`` / ``/page/`` increment to *n + 1* (Req 5.1 step 2c);
- the Locator's next-page ref ``href`` (Req 5.1 step 3);
- a relative ``href`` resolved to absolute (Req 5.3);
- an off-domain candidate rejected → falls through / no URL (Req 5.3);
- infinite scroll / "Load more" with no ``href`` → ``url=None`` with a reason,
  distinguished from no control at all (Req 5.4);
- precedence: plan rule beats generic beats Locator (Req 5.1 order);
- subdomains share one registrable domain (documented decision, Req 5.3).

Pagination is pure and AI-free; the suite-wide autouse ``FakeClaude`` fixture
keeps any incidental token counting offline.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extraction.models import (
    ExtractionPlan,
    LocatorNextPage,
    LocatorResult,
    NextPageRule,
)
from app.extraction.pagination import (
    REASON_NO_NEXT_PAGE,
    REASON_SCRIPT_DRIVEN,
    RULE_GENERIC_LABEL,
    RULE_GENERIC_PAGE_PARAM,
    RULE_GENERIC_REL_NEXT,
    RULE_LOCATOR_REF,
    RULE_NONE,
    RULE_PLAN_SELECTOR,
    RULE_PLAN_URL_TEMPLATE,
    infer_url_template,
    next_page,
    normalize_url,
)

_PAGE_URL = "https://reviews.example.com/products/acme?page=2"
_HOME = "https://reviews.example.com/products/acme"


def _plan(rule: NextPageRule) -> ExtractionPlan:
    """Build a minimal :class:`ExtractionPlan` carrying only ``rule``."""
    return ExtractionPlan(
        created_at=datetime(2024, 1, 1, tzinfo=UTC).isoformat(),
        method="selectors",
        next_page_rule=rule,
    )


# ---------------------------------------------------------------------------
# Plan rule — selector
# ---------------------------------------------------------------------------


def test_plan_selector_rule_yields_href() -> None:
    """The plan's selector rule applies the CSS and takes the element's href."""
    html = '<html><body><a class="pager-next" href="/products/acme?page=3">Next</a></body></html>'
    plan = _plan(NextPageRule(type="selector", css="a.pager-next"))

    result = next_page(html, _HOME, plan)

    assert result.rule_used == RULE_PLAN_SELECTOR
    assert result.url == "https://reviews.example.com/products/acme?page=3"
    assert result.reason_if_none is None


def test_plan_selector_rule_falls_through_when_no_match() -> None:
    """A selector rule that matches nothing falls through to generic patterns."""
    html = '<html><body><a rel="next" href="/products/acme?page=3">More</a></body></html>'
    plan = _plan(NextPageRule(type="selector", css="a.absent"))

    result = next_page(html, _HOME, plan)

    # Fell through to the generic rel=next link.
    assert result.rule_used == RULE_GENERIC_REL_NEXT
    assert result.url == "https://reviews.example.com/products/acme?page=3"


# ---------------------------------------------------------------------------
# Plan rule — url_template
# ---------------------------------------------------------------------------


def test_plan_url_template_fills_page_n_plus_1() -> None:
    """A url_template rule fills in page n + 1 inferred from the current URL."""
    plan = _plan(
        NextPageRule(
            type="url_template",
            template="https://reviews.example.com/products/acme?page={page}",
        )
    )

    result = next_page("<html><body></body></html>", _PAGE_URL, plan)

    assert result.rule_used == RULE_PLAN_URL_TEMPLATE
    # Current URL is page 2 → next is page 3.
    assert result.url == "https://reviews.example.com/products/acme?page=3"


def test_plan_url_template_defaults_current_page_to_one() -> None:
    """With no page marker on the current URL, the template resolves to page 2."""
    plan = _plan(
        NextPageRule(
            type="url_template",
            template="https://reviews.example.com/products/acme?page={page}",
        )
    )

    result = next_page("<html><body></body></html>", _HOME, plan)

    assert result.url == "https://reviews.example.com/products/acme?page=2"


# ---------------------------------------------------------------------------
# Generic patterns
# ---------------------------------------------------------------------------


def test_generic_rel_next() -> None:
    """A ``rel="next"`` link is found by the generic patterns."""
    html = '<html><body><a rel="next" href="/products/acme?page=3">continue</a></body></html>'

    result = next_page(html, _PAGE_URL, plan=None)

    assert result.rule_used == RULE_GENERIC_REL_NEXT
    assert result.url == "https://reviews.example.com/products/acme?page=3"


def test_generic_label_next_word() -> None:
    """A link labelled "Next" is matched by the label pattern."""
    html = '<html><body><a href="/products/acme?page=3">Next</a></body></html>'

    result = next_page(html, _PAGE_URL, plan=None)

    assert result.rule_used == RULE_GENERIC_LABEL
    assert result.url == "https://reviews.example.com/products/acme?page=3"


def test_generic_label_chevron() -> None:
    """A link labelled with the ``›`` glyph is matched by the label pattern."""
    html = '<html><body><a href="/products/acme?page=3">\u203a</a></body></html>'

    result = next_page(html, _PAGE_URL, plan=None)

    assert result.rule_used == RULE_GENERIC_LABEL
    assert result.url == "https://reviews.example.com/products/acme?page=3"


def test_generic_label_raquo() -> None:
    """A link labelled with the ``»`` glyph is matched by the label pattern."""
    html = '<html><body><a href="/products/acme?page=3">\u00bb</a></body></html>'

    result = next_page(html, _PAGE_URL, plan=None)

    assert result.rule_used == RULE_GENERIC_LABEL
    assert result.url == "https://reviews.example.com/products/acme?page=3"


def test_generic_page_param_query_increment() -> None:
    """A ``?page=`` link to exactly n + 1 is chosen; a non-adjacent one is not."""
    html = (
        "<html><body>"
        '<a href="/products/acme?page=9">last</a>'
        '<a href="/products/acme?page=3">three</a>'
        "</body></html>"
    )

    result = next_page(html, _PAGE_URL, plan=None)

    assert result.rule_used == RULE_GENERIC_PAGE_PARAM
    assert result.url == "https://reviews.example.com/products/acme?page=3"


def test_generic_page_param_p_short() -> None:
    """The ``&p=`` short form increments to n + 1 too."""
    url = "https://reviews.example.com/list?sort=new&p=4"
    html = '<html><body><a href="?sort=new&p=5">more</a></body></html>'

    result = next_page(html, url, plan=None)

    assert result.rule_used == RULE_GENERIC_PAGE_PARAM
    assert result.url == "https://reviews.example.com/list?sort=new&p=5"


def test_generic_page_param_path_increment() -> None:
    """A ``/page/<n+1>`` path link increments correctly."""
    url = "https://reviews.example.com/acme/page/2"
    html = '<html><body><a href="/acme/page/3">older reviews</a></body></html>'

    result = next_page(html, url, plan=None)

    assert result.rule_used == RULE_GENERIC_PAGE_PARAM
    assert result.url == "https://reviews.example.com/acme/page/3"


# ---------------------------------------------------------------------------
# Locator ref
# ---------------------------------------------------------------------------


def test_locator_ref_href() -> None:
    """When only the Locator points at a next-page element, its href is used."""
    # The second anchor is ``e3`` in the cleaner's document-order lookup.  Its
    # href carries no generic cue (no rel=next, no "Next" label, no page n+1
    # pattern), so only the Locator ref can produce it.
    html = (
        "<html><body><main>"
        '<a class="c">Reviews</a>'
        '<a class="pager" href="/products/acme/more">load</a>'
        "</main></body></html>"
    )
    locator = LocatorResult(next_page=LocatorNextPage(ref="e3"))

    result = next_page(html, _PAGE_URL, plan=None, locator=locator)

    assert result.rule_used == RULE_LOCATOR_REF
    assert result.url == "https://reviews.example.com/products/acme/more"


# ---------------------------------------------------------------------------
# Relative href resolution (Requirement 5.3)
# ---------------------------------------------------------------------------


def test_relative_href_resolved_to_absolute() -> None:
    """A relative next href is resolved to an absolute URL against the page."""
    html = '<html><body><a rel="next" href="page/3">next</a></body></html>'
    url = "https://reviews.example.com/products/acme/"

    result = next_page(html, url, plan=None)

    assert result.url == "https://reviews.example.com/products/acme/page/3"


# ---------------------------------------------------------------------------
# Off-domain rejection (Requirement 5.3)
# ---------------------------------------------------------------------------


def test_off_domain_candidate_rejected() -> None:
    """An off-registrable-domain next link is dropped; result has no URL."""
    html = (
        '<html><body><a rel="next" href="https://evil.test/products?page=3">Next</a></body></html>'
    )

    result = next_page(html, _PAGE_URL, plan=None)

    assert result.url is None
    assert result.rule_used == RULE_NONE
    # A control existed but offered no same-site URL.
    assert result.reason_if_none == REASON_SCRIPT_DRIVEN


def test_off_domain_falls_through_to_same_site_candidate() -> None:
    """An off-domain link is skipped so a later same-site link still wins."""
    html = (
        "<html><body>"
        '<a href="https://evil.test/products?page=3">Next</a>'
        '<a rel="next" href="/products/acme?page=3">continue</a>'
        "</body></html>"
    )

    result = next_page(html, _PAGE_URL, plan=None)

    assert result.rule_used == RULE_GENERIC_REL_NEXT
    assert result.url == "https://reviews.example.com/products/acme?page=3"


# ---------------------------------------------------------------------------
# No URL — reasons (Requirement 5.4)
# ---------------------------------------------------------------------------


def test_load_more_button_without_href_reports_script_driven() -> None:
    """A "Load more" button with no href → no URL, reason script_driven."""
    html = '<html><body><button class="load-more">Load more</button></body></html>'

    result = next_page(html, _PAGE_URL, plan=None)

    assert result.url is None
    assert result.rule_used == RULE_NONE
    assert result.reason_if_none == REASON_SCRIPT_DRIVEN


def test_next_button_without_href_reports_script_driven() -> None:
    """A "Next" button (no href) is a control but offers no URL."""
    html = "<html><body><button>Next</button></body></html>"

    result = next_page(html, _PAGE_URL, plan=None)

    assert result.url is None
    assert result.reason_if_none == REASON_SCRIPT_DRIVEN


def test_no_control_at_all_reports_no_next_page() -> None:
    """A page with no next control at all → reason no_next_page."""
    html = "<html><body><p>Only one page of reviews here.</p></body></html>"

    result = next_page(html, _PAGE_URL, plan=None)

    assert result.url is None
    assert result.rule_used == RULE_NONE
    assert result.reason_if_none == REASON_NO_NEXT_PAGE


def test_javascript_href_is_not_a_url() -> None:
    """A ``javascript:`` next link carries no navigable URL (script-driven)."""
    html = '<html><body><a rel="next" href="javascript:loadMore()">Next</a></body></html>'

    result = next_page(html, _PAGE_URL, plan=None)

    assert result.url is None
    assert result.reason_if_none == REASON_SCRIPT_DRIVEN


# ---------------------------------------------------------------------------
# Precedence (Requirement 5.1 order)
# ---------------------------------------------------------------------------


def test_plan_rule_beats_generic_and_locator() -> None:
    """The plan rule wins over a generic link and the Locator ref."""
    html = (
        "<html><body><main>"
        '<a class="planned" href="/products/acme?page=10">plan target</a>'
        '<a rel="next" href="/products/acme?page=3">generic</a>'
        '<a class="loc" href="/products/acme?page=4">locator</a>'
        "</main></body></html>"
    )
    plan = _plan(NextPageRule(type="selector", css="a.planned"))
    locator = LocatorResult(next_page=LocatorNextPage(ref="e4"))

    result = next_page(html, _PAGE_URL, plan, locator=locator)

    assert result.rule_used == RULE_PLAN_SELECTOR
    assert result.url == "https://reviews.example.com/products/acme?page=10"


def test_generic_beats_locator() -> None:
    """With no usable plan rule, a generic link wins over the Locator ref."""
    html = (
        "<html><body><main>"
        '<a rel="next" href="/products/acme?page=3">generic</a>'
        '<a class="loc" href="/products/acme?page=4">locator</a>'
        "</main></body></html>"
    )
    plan = _plan(NextPageRule(type="none"))
    locator = LocatorResult(next_page=LocatorNextPage(ref="e3"))

    result = next_page(html, _PAGE_URL, plan, locator=locator)

    assert result.rule_used == RULE_GENERIC_REL_NEXT
    assert result.url == "https://reviews.example.com/products/acme?page=3"


# ---------------------------------------------------------------------------
# Subdomain vs registrable-domain (documented decision, Requirement 5.3)
# ---------------------------------------------------------------------------


def test_subdomains_share_registrable_domain() -> None:
    """A next link on a sibling subdomain is same-site (same registrable domain)."""
    url = "https://www.example.com/reviews?page=2"
    html = '<html><body><a rel="next" href="https://shop.example.com/reviews?page=3">Next</a></body></html>'

    result = next_page(html, url, plan=None)

    assert result.rule_used == RULE_GENERIC_REL_NEXT
    assert result.url == "https://shop.example.com/reviews?page=3"


def test_different_registrable_domain_is_off_site() -> None:
    """Two different registrable domains are not same-site, even if related words."""
    url = "https://www.example.com/reviews?page=2"
    html = '<html><body><a rel="next" href="https://www.example.org/reviews?page=3">Next</a></body></html>'

    result = next_page(html, url, plan=None)

    assert result.url is None


# ---------------------------------------------------------------------------
# URL-template inference helper (Requirement 5.2)
# ---------------------------------------------------------------------------


def test_infer_url_template_query() -> None:
    """Two URLs differing only by a page query yield a ``{page}`` template."""
    template = infer_url_template(
        "https://example.com/r?page=1",
        "https://example.com/r?page=2",
    )
    assert template == "https://example.com/r?page={page}"


def test_infer_url_template_path() -> None:
    """Two URLs differing only by a page path segment yield a template."""
    template = infer_url_template(
        "https://example.com/r/page/1",
        "https://example.com/r/page/2",
    )
    assert template == "https://example.com/r/page/{page}"


def test_infer_url_template_rejects_multi_number_difference() -> None:
    """URLs differing by more than the page number are not a template."""
    template = infer_url_template(
        "https://example.com/2023/r?page=1",
        "https://example.com/2024/r?page=2",
    )
    assert template is None


def test_infer_url_template_rejects_different_host() -> None:
    """URLs on different hosts never form a template."""
    template = infer_url_template(
        "https://a.example.com/r?page=1",
        "https://b.other.com/r?page=2",
    )
    assert template is None


def test_infer_url_template_identical_is_none() -> None:
    """Identical URLs do not form a template (nothing varies)."""
    same = "https://example.com/r?page=1"
    assert infer_url_template(same, same) is None


def test_filled_template_round_trips_through_next_page() -> None:
    """An inferred template, used as a plan rule, resolves to the next page."""
    template = infer_url_template(
        "https://reviews.example.com/products/acme?page=1",
        "https://reviews.example.com/products/acme?page=2",
    )
    assert template is not None
    plan = _plan(NextPageRule(type="url_template", template=template))

    result = next_page("<html><body></body></html>", _PAGE_URL, plan)

    assert result.url == "https://reviews.example.com/products/acme?page=3"


# ---------------------------------------------------------------------------
# normalize_url helper
# ---------------------------------------------------------------------------


def test_normalize_url_drops_fragment() -> None:
    """``normalize_url`` strips a trailing fragment but keeps the query."""
    assert normalize_url("https://example.com/r?page=2#reviews") == "https://example.com/r?page=2"
