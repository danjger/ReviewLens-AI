"""Property-based tests for the page cleaner (``app.extraction.cleaner``).

Covers four of the Extraction Engine's correctness properties from
``.kiro/specs/review-extraction/design.md``:

- Property 2: Deterministic cleaning (Requirement 1.5).
- Property 3: Lookup integrity (Requirement 1.3).
- Property 4: Hidden content excluded (Requirement 1.1).
- Property 5: Chunks cover the page (Requirement 1.4).

The strategy below generates HTML review lists with random layouts, attributes,
hidden elements, nesting, and noise (per the design: "generated HTML review
lists with random layouts, attributes, and noise").  Token counting goes
through the instrumented AI client, which the suite-wide autouse fixture points
at the offline ``FakeClaude`` stub (~4 characters per token), so these tests run
without the network and the budget logic is exercised deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.extraction.cleaner import (
    _trim_boilerplate,
    build_clean_result,
    chunk_lines,
    count_tokens,
    resolve_ref,
)
from hypothesis import given
from hypothesis import strategies as st
from selectolax.parser import HTMLParser

# ---------------------------------------------------------------------------
# A sentinel string embedded only inside hidden/removed elements.  If it ever
# shows up in the Cleaned Page lines, Property 4 has been violated.
# ---------------------------------------------------------------------------

HIDDEN_MARKER = "SECRET_HIDDEN_PAYLOAD"


@dataclass
class GeneratedPage:
    """A generated HTML page plus the sentinels we expect never to leak."""

    html: str
    #: Marker strings placed inside hidden/removed subtrees.
    hidden_markers: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Leaf-content strategies
# ---------------------------------------------------------------------------

# Visible review-ish text: letters, digits, spaces.  Kept free of ``<``/``>``
# and quotes so the generated markup stays well-formed.
_visible_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters=" .,!",
    ),
    min_size=5,
    max_size=120,
)

# Short attribute values (also kept markup-safe).
_attr_value = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters=" .-",
    ),
    min_size=1,
    max_size=24,
)

_rating_value = st.integers(min_value=1, max_value=5)


@st.composite
def _rating_attrs(draw: st.DrawFn) -> str:
    """Draw a (possibly empty) set of rating-bearing attributes as a string."""
    pieces: list[str] = []
    if draw(st.booleans()):
        stars = draw(_rating_value)
        pieces.append(f'aria-label="{stars} out of 5 stars"')
    if draw(st.booleans()):
        pieces.append(f'data-rating="{draw(_rating_value)}"')
    if draw(st.booleans()):
        token = draw(st.sampled_from(["star-5", "rating-wrap", "star", "rating"]))
        pieces.append(f'class="col {token} muted"')
    if draw(st.booleans()):
        pieces.append(f'title="{draw(_attr_value)}"')
    return (" " + " ".join(pieces)) if pieces else ""


# ---------------------------------------------------------------------------
# Hidden / removed noise.  Each injects HIDDEN_MARKER into content that the
# cleaner must exclude: dropped tags, the ``hidden`` attribute, aria-hidden, or
# inline display:none.
# ---------------------------------------------------------------------------


@st.composite
def _hidden_block(draw: st.DrawFn) -> str:
    """Draw one hidden/removed element whose text contains HIDDEN_MARKER."""
    marker_text = f"{HIDDEN_MARKER} {draw(_visible_text)}"
    kind = draw(
        st.sampled_from(
            [
                "script",
                "style",
                "noscript",
                "iframe",
                "template",
                "hidden-attr",
                "aria-hidden",
                "display-none",
                "nested-hidden",
            ]
        )
    )
    if kind in ("script", "style", "noscript", "iframe", "template"):
        return f"<{kind}>{marker_text}</{kind}>"
    if kind == "hidden-attr":
        return f"<div hidden>{marker_text}</div>"
    if kind == "aria-hidden":
        return f'<p aria-hidden="true">{marker_text}</p>'
    if kind == "display-none":
        return f'<span style="display:none">{marker_text}</span>'
    # A hidden descendant folded under a visible ancestor: the visible ancestor
    # text must survive, but the hidden child's marker must not leak.
    visible = draw(_visible_text)
    return f"<div>{visible}<span hidden>{marker_text}</span></div>"


# ---------------------------------------------------------------------------
# Visible review blocks and the surrounding page.
# ---------------------------------------------------------------------------


@st.composite
def _review_block(draw: st.DrawFn) -> str:
    """Draw a visible review card with random attributes and nesting."""
    attrs = draw(_rating_attrs())
    body = draw(_visible_text)
    author = draw(_visible_text)
    tag = draw(st.sampled_from(["article", "div", "li", "section"]))
    inner = f'<p>{body}</p><span class="reviewer-name">{author}</span>'
    if draw(st.booleans()):
        # Occasionally nest the content one level deeper to vary tree shape.
        inner = f"<div>{inner}</div>"
    return f"<{tag}{attrs}>{inner}</{tag}>"


@st.composite
def _generated_page(draw: st.DrawFn) -> GeneratedPage:
    """Draw a full HTML review-list page with layout, hidden noise, and nesting.

    The page interleaves visible review blocks with hidden/removed noise and
    optionally wraps the reviews in boilerplate (header/nav/footer) and a
    ``main`` container, so the generated input space covers the layouts the
    cleaner's rules and chunking must handle.
    """
    n_reviews = draw(st.integers(min_value=1, max_value=8))
    reviews = [draw(_review_block()) for _ in range(n_reviews)]

    hidden_markers: list[str] = []
    noise: list[str] = []
    for _ in range(draw(st.integers(min_value=0, max_value=4))):
        block = draw(_hidden_block())
        noise.append(block)
        hidden_markers.append(HIDDEN_MARKER)

    # Interleave reviews and noise in document order.
    body_parts: list[str] = []
    for i, review in enumerate(reviews):
        body_parts.append(review)
        if i < len(noise):
            body_parts.append(noise[i])
    body_parts.extend(noise[len(reviews) :])
    inner = "".join(body_parts)

    if draw(st.booleans()):
        inner = f"<main>{inner}</main>"

    header = "<header>Site Logo Home About Contact</header>" if draw(st.booleans()) else ""
    nav = "<nav>Home Products Pricing Blog Docs</nav>" if draw(st.booleans()) else ""
    footer = "<footer>Copyright Terms Privacy Cookies</footer>" if draw(st.booleans()) else ""

    html = f"<html><body>{header}{nav}{inner}{footer}</body></html>"
    return GeneratedPage(html=html, hidden_markers=hidden_markers)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _refs_in(chunks: list[list[str]]) -> set[str]:
    """Return the set of reference IDs appearing across all chunks."""
    refs: set[str] = set()
    for chunk in chunks:
        for line in chunk:
            token = line.strip().split(" ", 1)[0]
            if token:
                refs.add(token)
    return refs


def _top_level_count(chunk: list[str]) -> int:
    """Count whole top-level elements in a chunk by shallowest indentation.

    A whole element is a group rooted at the chunk's minimum indentation depth;
    its deeper descendant lines travel with it.  This mirrors how the cleaner
    groups lines into indivisible elements, so a chunk holding a single element
    (which cannot be split) counts as one regardless of how many nested ref
    lines it has.
    """
    non_empty = [line for line in chunk if line.strip()]
    if not non_empty:
        return 0
    min_indent = min(len(line) - len(line.lstrip(" ")) for line in non_empty)
    return sum(1 for line in non_empty if (len(line) - len(line.lstrip(" "))) == min_indent)


# ---------------------------------------------------------------------------
# Property 2: Deterministic cleaning (Requirement 1.5)
# ---------------------------------------------------------------------------


@given(page=_generated_page())
def test_cleaning_is_deterministic(page: GeneratedPage) -> None:
    """Property 2: Deterministic cleaning.

    For any HTML, cleaning twice SHALL produce identical lines and reference
    IDs.
    Validates: Requirement 1.5
    """
    first = build_clean_result(page.html)
    second = build_clean_result(page.html)
    assert first.lines == second.lines
    assert first.lookup == second.lookup


# ---------------------------------------------------------------------------
# Property 3: Lookup integrity (Requirement 1.3)
# ---------------------------------------------------------------------------


@given(page=_generated_page())
def test_every_ref_resolves_to_exactly_one_element(page: GeneratedPage) -> None:
    """Property 3: Lookup integrity.

    For any HTML, every reference ID in the Cleaned Page SHALL resolve to
    exactly one element in the original DOM.
    Validates: Requirement 1.3
    """
    result = build_clean_result(page.html)
    tree = HTMLParser(page.html)
    for ref, css_path in result.lookup.items():
        # The resolver returns a single node for a known ref ...
        node = resolve_ref(page.html, result.lookup, ref)
        assert node is not None, f"ref {ref} did not resolve"
        # ... and the stored CSS path selects exactly one element in the DOM.
        matches = tree.css(css_path)
        assert len(matches) == 1, f"ref {ref} path {css_path!r} matched {len(matches)} elements"


# ---------------------------------------------------------------------------
# Property 4: Hidden content excluded (Requirement 1.1)
# ---------------------------------------------------------------------------


@given(page=_generated_page())
def test_hidden_content_never_appears(page: GeneratedPage) -> None:
    """Property 4: Hidden content excluded.

    For any HTML, text inside removed or hidden elements SHALL NOT appear in
    the Cleaned Page.
    Validates: Requirement 1.1
    """
    result = build_clean_result(page.html)
    joined = "\n".join(result.lines)
    assert HIDDEN_MARKER not in joined


# ---------------------------------------------------------------------------
# Property 5: Chunks cover the page (Requirement 1.4)
# ---------------------------------------------------------------------------


@given(page=_generated_page())
def test_chunks_cover_the_page_within_budget(page: GeneratedPage) -> None:
    """Property 5: Chunks cover the page.

    For any page over budget, every kept element SHALL appear in at least one
    chunk, and every chunk SHALL be within the budget.
    Validates: Requirement 1.4

    A budget below the whole-page token count is chosen so chunking is actually
    forced.  A single element larger than the budget cannot be split (elements
    are never broken), so the only chunks permitted to exceed the budget are
    those holding exactly one element — the documented over-size exception.
    """
    result = build_clean_result(page.html)
    lines = result.lines
    # Only a page with content to split exercises Property 5.
    total = count_tokens(lines)
    if total == 0:
        return

    # A small budget relative to the page forces splitting (FakeClaude's
    # ~4-chars-per-token estimate makes this deterministic).
    budget = max(1, total // 3)
    chunks = chunk_lines(lines, result.lookup, budget_tokens=budget)

    # If the budget still happens to fit the whole page (tiny pages), the page
    # is returned as a single untrimmed chunk and Property 5's over-budget
    # clause does not apply.
    if count_tokens(lines) <= budget:
        assert chunks == [lines]
        return

    # Coverage: every kept element appears in at least one chunk.  Over budget,
    # the cleaner first drops boilerplate (header/footer/nav/aside) that carries
    # no review cues, so "kept" means the elements that survive that trim; every
    # one of those must appear in some chunk, and no chunk may invent a ref.
    kept_refs = _refs_in([_trim_boilerplate(lines)])
    chunk_refs = _refs_in(chunks)
    assert chunk_refs == kept_refs, "chunks must cover exactly the kept elements"

    # Every chunk is within budget, except a chunk holding a single oversize
    # element (which cannot be split).
    for chunk in chunks:
        if not chunk:
            continue
        if count_tokens(chunk) > budget:
            assert _top_level_count(chunk) <= 1, (
                "only a single oversize element may exceed the budget"
            )
