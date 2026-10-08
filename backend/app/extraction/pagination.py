"""Next-page resolution for the Extraction Engine (Requirement 5).

``next_page`` decides where the review list continues after the current page.
It is called by the processing Worker for every captured page, and composes the
three sources of a next-page candidate in the order Requirement 5.1 mandates:

1. **The plan's next-page rule** (``plan.next_page_rule``), when a plan exists:
   - ``type == "selector"`` → apply the CSS to the HTML and take the matched
     element's ``href``;
   - ``type == "url_template"`` → fill in page *n + 1*, inferring the current
     page number *n* from the current URL;
   - ``type == "none"`` → skip to the next source.
2. **Generic patterns**, in a fixed sub-order:
   - a ``rel="next"`` link;
   - a link or button labelled ``Next``, ``›``, or ``»``;
   - a link to page *n + 1* by ``?page=``, ``&p=``, or ``/page/``.
3. **The Locator's next-page element** (``locator.next_page.ref``), when the
   page was read by the Locator: resolve the ref to its element and take its
   ``href``.

The first source that yields a candidate wins (Requirement 5.1).  Every
candidate is resolved to an absolute URL against the current URL
(:func:`urllib.parse.urljoin`) and must be on the **same registrable domain**
as the page's final URL (Requirement 5.3); off-domain candidates are dropped
and resolution continues to the next source.  When no source yields a usable
same-domain URL, ``next_page`` reports *why* (Requirement 5.4): a next/"Load
more" control that carries no URL is ``script_driven_no_url``; the absence of
any next control is ``no_next_page``.

Same-site definition (Requirement 5.3): two URLs are same-site when they share
the same **registrable domain** (eTLD+1) as computed by ``tldextract`` with its
**bundled** suffix snapshot (no network).  Subdomains therefore count as the
same site: ``https://shop.example.com/...`` is same-site as
``https://www.example.com/...`` (both ``example.com``).  This is deliberate —
review listings routinely paginate across ``www``/bare-host/CDN subdomains of
one registrable domain — and is what Property 7 asserts.

**SSRF is the caller's job.**  The design is explicit: pagination only filters
to the same registrable domain and never calls ``assert_public_host`` — the
caller (which performs the actual fetch) is responsible for the SSRF check.

``rule_used`` vocabulary (also documented on :func:`next_page`):

======================  ====================================================
``rule_used``           Produced by
======================  ====================================================
``plan_selector``       plan rule ``type == "selector"`` matched an ``href``
``plan_url_template``   plan rule ``type == "url_template"`` filled page n+1
``generic_rel_next``    a ``rel="next"`` link
``generic_label``       a link/button labelled Next / › / »
``generic_page_param``  a ``?page=`` / ``&p=`` / ``/page/`` link to page n+1
``locator_ref``         the Locator's ``next_page.ref`` element ``href``
``none``                no same-domain URL was found (see ``reason_if_none``)
======================  ====================================================
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import tldextract
from selectolax.parser import HTMLParser, Node

from app.extraction.cleaner import resolve_ref
from app.extraction.models import ExtractionPlan, LocatorResult, NextPage, NextPageRule

# ---------------------------------------------------------------------------
# rule_used vocabulary
# ---------------------------------------------------------------------------

RULE_PLAN_SELECTOR = "plan_selector"
RULE_PLAN_URL_TEMPLATE = "plan_url_template"
RULE_GENERIC_REL_NEXT = "generic_rel_next"
RULE_GENERIC_LABEL = "generic_label"
RULE_GENERIC_PAGE_PARAM = "generic_page_param"
RULE_LOCATOR_REF = "locator_ref"
RULE_NONE = "none"

# ---------------------------------------------------------------------------
# reason_if_none vocabulary
# ---------------------------------------------------------------------------

#: A next / "Load more" control exists but carries no URL (infinite scroll or a
#: script-driven button) — Requirement 5.4.
REASON_SCRIPT_DRIVEN = "script_driven_no_url"

#: No next-page control of any kind was found on the page.
REASON_NO_NEXT_PAGE = "no_next_page"

# ---------------------------------------------------------------------------
# Offline registrable-domain extractor (bundled suffix snapshot, no network)
# ---------------------------------------------------------------------------

#: ``tldextract`` configured with no suffix-list URLs, so it uses only the
#: snapshot bundled in the package and never touches the network (Requirement
#: 5.3 / design: "bundled suffix list, so no network access").
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())

# ---------------------------------------------------------------------------
# Generic-pattern tables
# ---------------------------------------------------------------------------

#: Visible labels (normalised to lower-case, stripped) that mark a "next" link
#: or button (Requirement 5.1).  ``next`` matches a label that *is* "next" or
#: begins "next " (e.g. "Next page"), handled in :func:`_label_is_next`.
_NEXT_SYMBOLS: frozenset[str] = frozenset({"›", "»", "→", "next ›", "next »"})

#: Query/path page-number patterns.  Each captures the integer page number so a
#: candidate can be confirmed to point at page *n + 1* (Requirement 5.1).
_PAGE_QUERY_RE = re.compile(r"[?&](?:page|p)=(\d+)\b", re.IGNORECASE)
_PAGE_PATH_RE = re.compile(r"/page/(\d+)\b", re.IGNORECASE)

#: A ``{page}`` or ``{n}`` placeholder in a URL template (either is accepted so
#: callers / plan derivation are not forced to a single spelling).
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{(?:page|n)\}")

#: A run of digits anywhere in a URL — used by URL-template inference to find
#: the single varying page number between two successive page URLs.
_DIGITS_RE = re.compile(r"\d+")


# ---------------------------------------------------------------------------
# Domain helpers (Requirement 5.3)
# ---------------------------------------------------------------------------


def _registrable_domain(url: str) -> str:
    """Return the registrable domain (eTLD+1) of ``url`` using the snapshot.

    Prefers ``top_domain_under_public_suffix`` (the current ``tldextract``
    spelling) and falls back to the older ``registered_domain`` attribute, so
    the comparison works across ``tldextract`` versions.  Returns ``""`` for a
    host with no registrable domain (an IP address, ``localhost``, or an
    unparseable URL), which never compares equal to a real host.
    """
    extracted = _EXTRACT(url)
    domain = getattr(extracted, "top_domain_under_public_suffix", None)
    if domain is None:
        domain = getattr(extracted, "registered_domain", "")
    return domain or ""


def _same_registrable_domain(a: str, b: str) -> bool:
    """Return ``True`` when ``a`` and ``b`` share a non-empty registrable domain.

    Subdomains count as the same site (see the module docstring).  Two URLs with
    no registrable domain (IPs, ``localhost``) never match, so a candidate that
    cannot be attributed to a registrable domain is treated as off-site.
    """
    domain_a = _registrable_domain(a)
    domain_b = _registrable_domain(b)
    return bool(domain_a) and domain_a == domain_b


def _absolutize(base_url: str, href: str) -> str | None:
    """Resolve ``href`` against ``base_url`` to an absolute ``http(s)`` URL.

    Returns ``None`` when ``href`` is empty/whitespace or does not resolve to an
    HTTP(S) URL (for example ``javascript:``, ``#``, or ``mailto:`` links, which
    are script-driven or non-navigational and carry no next page).
    """
    candidate = (href or "").strip()
    if not candidate:
        return None
    absolute = urljoin(base_url, candidate)
    scheme = urlsplit(absolute).scheme.lower()
    if scheme not in ("http", "https"):
        return None
    return absolute


def _same_site_or_none(base_url: str, href: str) -> str | None:
    """Absolutise ``href`` and keep it only if it is on ``base_url``'s domain.

    Combines :func:`_absolutize` and :func:`_same_registrable_domain`: returns
    the absolute URL when it is a usable same-registrable-domain HTTP(S) URL,
    otherwise ``None`` (so the caller falls through to the next rule).
    """
    absolute = _absolutize(base_url, href)
    if absolute is None:
        return None
    if not _same_registrable_domain(base_url, absolute):
        return None
    return absolute


# ---------------------------------------------------------------------------
# Element helpers
# ---------------------------------------------------------------------------


def _href_of(node: Node | None) -> str | None:
    """Return the ``href`` attribute of ``node`` (``None`` when absent)."""
    if node is None:
        return None
    return node.attributes.get("href")


def _label_is_next(label: str) -> bool:
    """Return ``True`` when a normalised control label denotes "next".

    Matches the word "Next" (exactly, or as the first word of e.g. "Next page")
    and the next-arrow glyphs ``›`` / ``»`` / ``→`` (Requirement 5.1).  Matching
    is case-insensitive and whitespace-tolerant.
    """
    text = label.strip().lower()
    if not text:
        return False
    if text in _NEXT_SYMBOLS:
        return True
    if text == "next" or text.startswith("next ") or text.startswith("next\t"):
        return True
    # A label that is just an arrow glyph, possibly with surrounding spaces.
    return text in {"›", "»", "→"}


# ---------------------------------------------------------------------------
# Plan rule (Requirement 5.1 step 1)
# ---------------------------------------------------------------------------


def _page_number(url: str) -> int:
    """Infer the current page number from ``url`` (defaults to 1).

    Reads ``?page=`` / ``&p=`` first, then ``/page/<n>``.  A URL with no page
    marker is page 1, so ``url_template`` resolution produces page 2 next.
    """
    query_match = _PAGE_QUERY_RE.search(url)
    if query_match is not None:
        return int(query_match.group(1))
    path_match = _PAGE_PATH_RE.search(url)
    if path_match is not None:
        return int(path_match.group(1))
    return 1


def _fill_template(template: str, page: int) -> str | None:
    """Fill a URL template's ``{page}``/``{n}`` placeholder with ``page``.

    Returns the filled URL, or ``None`` when the template has no recognised
    placeholder (so it cannot be a page-number template).
    """
    if _TEMPLATE_PLACEHOLDER_RE.search(template) is None:
        return None
    return _TEMPLATE_PLACEHOLDER_RE.sub(str(page), template)


def _resolve_plan_rule(
    tree: HTMLParser,
    url: str,
    rule: NextPageRule,
) -> tuple[str | None, str] | None:
    """Resolve the plan's next-page rule to ``(url_or_none, rule_used)``.

    Returns ``None`` when the rule does not apply at all (``type == "none"``, or
    a selector/template that produced nothing), so the caller falls through to
    the generic patterns.  Returns ``(absolute_url, rule_used)`` on a same-site
    hit.  Returns ``(None, rule_used)`` only when the rule *fired* but its target
    was off-domain or unusable — still signalling "fall through" to the caller
    (which treats a ``None`` url as no result), while naming the rule for logs.
    """
    if rule.type == "selector" and rule.css:
        node = tree.css_first(rule.css)
        absolute = _same_site_or_none(url, _href_of(node) or "")
        if absolute is not None:
            return absolute, RULE_PLAN_SELECTOR
        return None
    if rule.type == "url_template" and rule.template:
        filled = _fill_template(rule.template, _page_number(url) + 1)
        if filled is None:
            return None
        absolute = _same_site_or_none(url, filled)
        if absolute is not None:
            return absolute, RULE_PLAN_URL_TEMPLATE
        return None
    return None


# ---------------------------------------------------------------------------
# Generic patterns (Requirement 5.1 step 2)
# ---------------------------------------------------------------------------


def _find_rel_next(tree: HTMLParser, url: str) -> str | None:
    """Return the same-site ``href`` of a ``rel="next"`` link, if any."""
    for node in tree.css("a[rel], link[rel]"):
        rel = (node.attributes.get("rel") or "").lower()
        if "next" in rel.split():
            absolute = _same_site_or_none(url, _href_of(node) or "")
            if absolute is not None:
                return absolute
    return None


def _find_labelled_next(tree: HTMLParser, url: str) -> str | None:
    """Return the same-site ``href`` of a link labelled Next / › / », if any.

    Only ``<a>`` elements can carry a navigable URL; a ``<button>`` labelled
    "Next" with no ``href`` is script-driven and handled by :func:`_has_control`
    as a no-URL control (Requirement 5.4).
    """
    for node in tree.css("a"):
        if _label_is_next(node.text()):
            absolute = _same_site_or_none(url, _href_of(node) or "")
            if absolute is not None:
                return absolute
    return None


def _find_page_param_next(tree: HTMLParser, url: str) -> str | None:
    """Return a same-site link to page *n + 1* via ``?page=``/``&p=``/``/page/``.

    Scans anchors whose resolved absolute URL carries a page-number marker and
    returns the first that points at exactly ``current + 1`` (Requirement 5.1).
    Resolving before matching means a relative ``?page=3`` link is still read.
    """
    target = _page_number(url) + 1
    for node in tree.css("a"):
        absolute = _same_site_or_none(url, _href_of(node) or "")
        if absolute is None:
            continue
        query_match = _PAGE_QUERY_RE.search(absolute)
        path_match = _PAGE_PATH_RE.search(absolute)
        number = None
        if query_match is not None:
            number = int(query_match.group(1))
        elif path_match is not None:
            number = int(path_match.group(1))
        if number == target:
            return absolute
    return None


def _resolve_generic(tree: HTMLParser, url: str) -> tuple[str, str] | None:
    """Resolve the generic patterns in order; return ``(url, rule_used)``."""
    rel_next = _find_rel_next(tree, url)
    if rel_next is not None:
        return rel_next, RULE_GENERIC_REL_NEXT
    labelled = _find_labelled_next(tree, url)
    if labelled is not None:
        return labelled, RULE_GENERIC_LABEL
    page_param = _find_page_param_next(tree, url)
    if page_param is not None:
        return page_param, RULE_GENERIC_PAGE_PARAM
    return None


# ---------------------------------------------------------------------------
# Locator ref (Requirement 5.1 step 3)
# ---------------------------------------------------------------------------


def _resolve_locator(
    html: str,
    url: str,
    locator: LocatorResult | None,
) -> tuple[str, str] | None:
    """Resolve the Locator's next-page ref to a same-site ``href``.

    The Locator's ``next_page.ref`` is an ``eN`` id from the cleaner's lookup.
    ``next_page`` is not given that lookup, so this rebuilds it deterministically
    from the same HTML (cleaning is pure — Requirement 1.5), resolves the ref to
    its original element, and keeps the element's ``href`` only when it is a
    same-registrable-domain URL.  Returns ``None`` (fall through) when there is
    no ref, it does not resolve, or its ``href`` is off-domain / unusable.
    """
    if locator is None or locator.next_page.ref is None:
        return None
    # The Locator ref is an ``eN`` id from the cleaner's lookup.  Rebuild the
    # lookup from the same HTML so the ref resolves to the original element.
    from app.extraction.cleaner import build_clean_result  # noqa: PLC0415 - avoid import cycle

    lookup = build_clean_result(html).lookup
    node = resolve_ref(html, lookup, locator.next_page.ref)
    absolute = _same_site_or_none(url, _href_of(node) or "")
    if absolute is not None:
        return absolute, RULE_LOCATOR_REF
    return None


# ---------------------------------------------------------------------------
# "No URL" reason (Requirement 5.4)
# ---------------------------------------------------------------------------


def _has_next_control(tree: HTMLParser, locator: LocatorResult | None) -> bool:
    """Return ``True`` when the page has *some* next/"Load more" control.

    Used only to choose the ``reason_if_none`` when no URL was resolved: a
    control exists (a labelled button/link, a Locator next-page ref, or a "load
    more" affordance) but offers no navigable URL → ``script_driven_no_url``;
    nothing at all → ``no_next_page`` (Requirement 5.4).
    """
    if locator is not None and locator.next_page.ref is not None:
        return True
    for node in tree.css("a, button"):
        text = node.text().strip().lower()
        if _label_is_next(node.text()):
            return True
        if "load more" in text or "show more" in text or "more reviews" in text:
            return True
    return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def next_page(
    html: str,
    url: str,
    plan: ExtractionPlan | None,
    locator: LocatorResult | None = None,
) -> NextPage:
    """Resolve the next-page candidate for a page (Requirement 5).

    Applies, in order (first hit wins): the plan's next-page rule, the generic
    patterns, then the Locator's next-page element.  Every candidate is resolved
    to an absolute URL against ``url`` and kept only if it is on the same
    registrable domain (Requirement 5.3); off-domain candidates are dropped and
    resolution continues.  When no source yields a same-domain URL, the reason
    is reported (Requirement 5.4).

    :param html: Rendered HTML of the page.
    :param url: The page's final URL (candidates are resolved against it and
        filtered to its registrable domain).
    :param plan: The Extraction Plan, when one exists; its ``next_page_rule`` is
        tried first.
    :param locator: The Locator result for this page, when it was read by the
        Locator; its ``next_page.ref`` is the last source tried.
    :returns: A :class:`NextPage`.  ``url`` is the absolute same-domain next URL
        or ``None``; ``rule_used`` names the producing rule (see the module
        docstring for the vocabulary) or ``"none"``; ``reason_if_none`` is set
        only when ``url`` is ``None``.
    """
    tree = HTMLParser(html)

    # 1. Plan rule.
    if plan is not None:
        resolved = _resolve_plan_rule(tree, url, plan.next_page_rule)
        if resolved is not None and resolved[0] is not None:
            return NextPage(url=resolved[0], rule_used=resolved[1])

    # 2. Generic patterns.
    generic = _resolve_generic(tree, url)
    if generic is not None:
        return NextPage(url=generic[0], rule_used=generic[1])

    # 3. Locator ref.
    located = _resolve_locator(html, url, locator)
    if located is not None:
        return NextPage(url=located[0], rule_used=located[1])

    # 4. No URL — report why (Requirement 5.4).
    reason = REASON_SCRIPT_DRIVEN if _has_next_control(tree, locator) else REASON_NO_NEXT_PAGE
    return NextPage(url=None, rule_used=RULE_NONE, reason_if_none=reason)


# ---------------------------------------------------------------------------
# URL-template inference (Requirement 5.2 helper, reusable by plan derivation)
# ---------------------------------------------------------------------------


def infer_url_template(first_url: str, second_url: str) -> str | None:
    """Infer a page-number URL template from two successive page URLs.

    When ``first_url`` and ``second_url`` differ **only** by a single run of
    digits (the page number), returns a template with that run replaced by a
    ``{page}`` placeholder (Requirement 5.2).  Returns ``None`` when the two
    URLs are identical, differ in more than the page number, or share no single
    differing digit run — in which case a CSS selector rule is the right choice
    instead.

    This is kept here so plan derivation (``plan.py``) and callers can reuse one
    implementation; :func:`next_page` fills such a template via the plan's
    ``url_template`` rule.  Purely syntactic and deterministic — no network.
    """
    if first_url == second_url:
        return None

    # The scheme/host must match for a template to make sense.
    first_parts = urlsplit(first_url)
    second_parts = urlsplit(second_url)
    if (first_parts.scheme, first_parts.netloc) != (second_parts.scheme, second_parts.netloc):
        return None

    first_digits = list(_DIGITS_RE.finditer(first_url))
    second_digits = list(_DIGITS_RE.finditer(second_url))

    # Replace every digit run with a sentinel; the surrounding text must be
    # identical and the runs must align one-to-one for the URLs to differ only
    # by numbers.
    first_skeleton = _DIGITS_RE.sub("\x00", first_url)
    second_skeleton = _DIGITS_RE.sub("\x00", second_url)
    if first_skeleton != second_skeleton:
        return None
    if len(first_digits) != len(second_digits):
        return None

    # Find exactly one digit run that changed between the two URLs.
    differing = [
        index
        for index, (a, b) in enumerate(zip(first_digits, second_digits, strict=True))
        if a.group(0) != b.group(0)
    ]
    if len(differing) != 1:
        return None

    changed = second_digits[differing[0]]
    start, end = changed.span()
    return second_url[:start] + "{page}" + second_url[end:]


def normalize_url(url: str) -> str:
    """Return ``url`` with an empty fragment dropped (small canonicaliser).

    Helper for callers that want to compare or store next-page URLs without a
    trailing ``#`` fragment; not used by :func:`next_page` itself but kept with
    the pagination helpers so URL handling lives in one module.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
