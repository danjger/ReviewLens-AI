"""Page cleaner for the Extraction Engine.

Turns rendered HTML into a compact, reference-tagged :class:`CleanedPage` the
Review Locator can read cheaply and point at by element.  Task 2.1 added the
removal rules, kept attributes, reference IDs, the ref-to-element lookup, and
line rendering.  Task 2.2 completes the cleaner with token counting (via the
instrumented Anthropic token counter, cached per page), boilerplate trimming,
and chunking of whole elements with a two-element overlap when a page exceeds
the token budget.

Design: see ``.kiro/specs/review-extraction/design.md`` ("Page cleaner").

Determinism (Requirement 1.5 / Property 2): parsing and traversal are fully
deterministic, so the same HTML always yields identical lines and reference IDs.
The ref-to-element lookup stores a CSS path into the *original* DOM, so every
reference resolves back to exactly one element (Requirement 1.3 / Property 3).
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser, Node

# ---------------------------------------------------------------------------
# Rule tables
# ---------------------------------------------------------------------------

# Elements whose entire subtree is dropped (content and children).  ``svg`` is
# included so its (often large) child markup never reaches the Locator.
_DROP_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
        "template",
    }
)

# Block-level tags that receive a reference ID when kept.  ``a``/``area`` are
# included so link targets (``href``) reach the Cleaned Page — the Locator needs
# them to point at next-page controls.  Purely inline tags (``span``,
# ``strong`` …) contribute their text and attributes to the nearest ancestor
# block but do not get their own ref, which keeps the Cleaned Page compact.
_BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "a",
        "address",
        "area",
        "article",
        "aside",
        "blockquote",
        "button",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hgroup",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "section",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "time",
        "tr",
        "ul",
    }
)

# Attributes always kept on a block when present: they carry rating cues,
# accessible labels, link targets, and microdata.
_KEEP_ATTRS: tuple[str, ...] = (
    "href",
    "aria-label",
    "title",
    "alt",
    "itemprop",
)

# An attribute name containing one of these substrings is kept (e.g.
# ``data-rating``, ``data-score``, ``aria-rating``).
_RATING_ATTR_SUBSTRINGS: tuple[str, ...] = ("rating", "score")

# A class token containing one of these substrings is kept (e.g. ``star-5``,
# ``rating-wrap``).
_RATING_CLASS_SUBSTRINGS: tuple[str, ...] = ("star", "rating")

# Inline ``style`` declaration that hides an element.
_DISPLAY_NONE_RE = re.compile(r"display\s*:\s*none", re.IGNORECASE)

# Collapse any run of whitespace (including newlines) to a single space.
_WS_RE = re.compile(r"\s+")

# Length a rendered text snippet is truncated to on a line.
_TEXT_SNIPPET_LEN = 200


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class CleanResult:
    """Internal cleaner output before it is packed into a :class:`CleanedPage`.

    ``lines`` are the rendered element lines in document order.  ``lookup`` maps
    each reference ID to a CSS path into the *original* DOM.  ``original_html``
    is retained so :meth:`resolve` can re-parse and return the matching element.
    """

    lines: list[str] = field(default_factory=list)
    lookup: dict[str, str] = field(default_factory=dict)
    original_html: str = ""

    def resolve(self, ref: str) -> Node | None:
        """Resolve a reference ID back to its element in the original DOM.

        Re-parses ``original_html`` and applies the stored CSS path.  Returns
        the single matching :class:`~selectolax.parser.Node`, or ``None`` when
        the ref is unknown.  The CSS paths this cleaner emits use ``:nth-of-type``
        all the way to the root, so a known ref always selects exactly one
        element (Requirement 1.3 / Property 3).

        :param ref: A reference ID such as ``"e3"``.
        :returns: The resolved node, or ``None`` if ``ref`` is not in the lookup.
        """
        css_path = self.lookup.get(ref)
        if css_path is None:
            return None
        tree = HTMLParser(self.original_html)
        return tree.css_first(css_path)


# ---------------------------------------------------------------------------
# Hidden / removal predicates
# ---------------------------------------------------------------------------


def _is_hidden(node: Node) -> bool:
    """Return ``True`` when ``node`` is hidden and must be excluded.

    Hidden means a ``hidden`` attribute, ``aria-hidden="true"``, or an inline
    ``display:none`` style (Requirement 1.1).
    """
    attrs = node.attributes
    if "hidden" in attrs:
        return True
    if (attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    style = attrs.get("style")
    if style and _DISPLAY_NONE_RE.search(style):
        return True
    return False


def _is_dropped(node: Node) -> bool:
    """Return ``True`` when the node's whole subtree should be removed."""
    tag = node.tag
    if tag in _DROP_TAGS:
        return True
    return _is_hidden(node)


# ---------------------------------------------------------------------------
# Attribute selection and text extraction
# ---------------------------------------------------------------------------


def _kept_attributes(node: Node) -> list[tuple[str, str]]:
    """Return the rating-bearing / structural attributes kept for a block.

    Keeps the fixed set (``href``, ``aria-label``, ``title``, ``alt``,
    ``itemprop``), any attribute whose name contains ``rating`` or ``score``,
    and the ``class`` attribute reduced to tokens containing ``star`` or
    ``rating`` (Requirement 1.2).  Order is deterministic: fixed attributes in
    declared order, then rating/score attributes in document order, then the
    filtered class tokens.
    """
    attrs = node.attributes
    kept: list[tuple[str, str]] = []
    seen: set[str] = set()

    for name in _KEEP_ATTRS:
        value = attrs.get(name)
        if value is not None and name not in seen:
            kept.append((name, value))
            seen.add(name)

    for name, value in attrs.items():
        if name in seen or value is None:
            continue
        lowered = name.lower()
        if any(sub in lowered for sub in _RATING_ATTR_SUBSTRINGS):
            kept.append((name, value))
            seen.add(name)

    class_value = attrs.get("class")
    if class_value:
        tokens = [
            token
            for token in class_value.split()
            if any(sub in token.lower() for sub in _RATING_CLASS_SUBSTRINGS)
        ]
        if tokens:
            kept.append(("class", " ".join(tokens)))

    return kept


def _own_text(node: Node) -> str:
    """Return a block's own visible text, excluding dropped/hidden descendants.

    Walks the node's descendants in document order, concatenating text nodes but
    skipping any subtree that is dropped or hidden, so hidden content never
    reaches the Cleaned Page (Requirement 1.1 / Property 4).  Whitespace is
    normalized to single spaces.
    """
    parts: list[str] = []

    def walk(current: Node) -> None:
        for child in current.iter(include_text=True):
            if child.tag == "-text":
                text = child.text(deep=False)
                if text:
                    parts.append(text)
                continue
            if _is_dropped(child):
                continue
            walk(child)

    walk(node)
    joined = "".join(parts)
    return _WS_RE.sub(" ", joined).strip()


# ---------------------------------------------------------------------------
# CSS path (original DOM)
# ---------------------------------------------------------------------------


def _css_path(node: Node) -> str:
    """Build an ``:nth-of-type`` CSS path from the document root to ``node``.

    The path is absolute and uses ``tag:nth-of-type(n)`` at every level, so it
    selects exactly one element in the original DOM regardless of ids or
    classes (which may be absent or duplicated).  Deterministic for identical
    HTML.
    """
    segments: list[str] = []
    current: Node | None = node
    while current is not None:
        tag = current.tag
        parent = current.parent
        # Stop once we reach the ``html`` root (its parent is the synthetic
        # ``-undef`` document node) or run out of real ancestors.
        if parent is None or parent.tag in ("-undef", None):
            segments.append(tag)
            break
        index = 1
        sibling = current.prev
        while sibling is not None:
            if sibling.tag == tag:
                index += 1
            sibling = sibling.prev
        segments.append(f"{tag}:nth-of-type({index})")
        current = parent
    segments.reverse()
    return " > ".join(segments)


# ---------------------------------------------------------------------------
# Line rendering
# ---------------------------------------------------------------------------


def _render_line(ref: str, node: Node, depth: int, text: str) -> str:
    """Render one Cleaned Page line.

    Format (per the design example)::

        e123 <div class="review-card"> "text…" [aria-label="5 out of 5 stars"]

    Indentation reflects block nesting depth.  Kept attributes are rendered as a
    ``name="value"`` open tag plus a trailing ``[...]`` group; the text snippet
    is truncated to keep lines small.
    """
    indent = "  " * depth
    attrs = _kept_attributes(node)
    attr_str = "".join(f' {name}="{value}"' for name, value in attrs)
    open_tag = f"<{node.tag}{attr_str}>"

    pieces = [f"{indent}{ref} {open_tag}"]
    if text:
        snippet = text[:_TEXT_SNIPPET_LEN]
        if len(text) > _TEXT_SNIPPET_LEN:
            snippet += "…"
        pieces.append(f'"{snippet}"')
    if attrs:
        bracket = " ".join(f'{name}="{value}"' for name, value in attrs)
        pieces.append(f"[{bracket}]")
    return " ".join(pieces)


# ---------------------------------------------------------------------------
# Core traversal
# ---------------------------------------------------------------------------


def resolve_ref(html: str, lookup: dict[str, str], ref: str) -> Node | None:
    """Resolve a reference ID to its element in the original DOM.

    The companion to a :class:`CleanedPage`: given the page's original HTML and
    its ``lookup``, apply the stored CSS path and return the single matching
    element, or ``None`` when the ref is unknown.  Post-processing (Task 4.2)
    uses this to read review text from referenced elements by code — the AI
    never supplies text.

    :param html: The original rendered HTML the Cleaned Page was built from.
    :param lookup: The ref-to-CSS-path mapping from the Cleaned Page.
    :param ref: A reference ID such as ``"e3"``.
    :returns: The resolved node, or ``None`` if ``ref`` is not in the lookup.
    """
    css_path = lookup.get(ref)
    if css_path is None:
        return None
    tree = HTMLParser(html)
    return tree.css_first(css_path)


def build_clean_result(html: str) -> CleanResult:
    """Clean ``html`` into lines, a ref lookup, and a resolver.

    Parses leniently with selectolax, walks the body in document order, drops
    unwanted and hidden subtrees, assigns ``e1``, ``e2`` … to kept block
    elements, records each ref's CSS path into the original DOM, and renders one
    line per kept block.  Pure and deterministic (Requirement 1.5).

    :param html: Rendered HTML of the page.
    :returns: The :class:`CleanResult` with lines, lookup, and the original HTML
        needed to resolve refs.
    """
    result = CleanResult(original_html=html)
    tree = HTMLParser(html)
    body = tree.body
    if body is None:
        return result

    counter = 0

    def visit(node: Node, depth: int) -> None:
        nonlocal counter
        for child in node.iter():
            if _is_dropped(child):
                continue
            if child.tag in _BLOCK_TAGS:
                counter += 1
                ref = f"e{counter}"
                result.lookup[ref] = _css_path(child)
                text = _own_text(child)
                result.lines.append(_render_line(ref, child, depth, text))
                visit(child, depth + 1)
            else:
                # Inline / unknown element: descend without assigning a ref so
                # its block descendants still get refs at the right depth.
                visit(child, depth)

    visit(body, 0)
    return result


# ---------------------------------------------------------------------------
# Token counting (Anthropic token counter, cached per page)
# ---------------------------------------------------------------------------

#: Purpose used for token counting so it resolves the extract model — the model
#: a Cleaned Page is actually sent to, so the count reflects reality.
_TOKEN_COUNT_PURPOSE = "extract_locator"  # noqa: S105 - AI purpose label, not a secret

#: Number of whole top-level elements overlapped between consecutive chunks, so
#: a review split across a chunk boundary is still fully present in one chunk
#: (design: "a two-element overlap").
_CHUNK_OVERLAP = 2

#: Top-level boilerplate blocks dropped first when a page is over budget,
#: provided they carry no review cues (Requirement 1.4).
_BOILERPLATE_TAGS: frozenset[str] = frozenset({"header", "footer", "nav", "aside"})

#: Substrings that mark a line as carrying a review cue, so a boilerplate block
#: containing one is kept rather than trimmed.  These mirror the rating-bearing
#: attributes and class tokens the cleaner keeps.
_CUE_SUBSTRINGS: tuple[str, ...] = (
    "aria-label=",
    "itemprop=",
    "rating",
    "score",
    "star",
)

# Matches the opening ``eN <tag`` of a rendered line, capturing the tag name.
_LINE_TAG_RE = re.compile(r"^\s*e\d+ <([a-zA-Z0-9]+)")


def _page_text(lines: list[str]) -> str:
    """Join Cleaned Page lines into the single string sent to the counter."""
    return "\n".join(lines)


def count_tokens(lines: list[str]) -> int:
    """Count the tokens a Cleaned Page (or chunk) would cost.

    Measures the rendered lines with the Anthropic token counter, routed through
    the single instrumented Claude client (steering: every AI call goes through
    it), so the count reflects what the Locator will actually be sent and so
    tests swap in :class:`FakeClaude` with no network.  The result is memoized
    per unique page text (design: "cached per page"), so repeated calls for the
    same page — common when :func:`clean` sizes the page and then chunking
    re-checks it — do not re-count.

    :param lines: The rendered Cleaned Page lines.
    :returns: The input-token count of the joined lines (``0`` for an empty page,
        counted without an AI call).
    """
    text = _page_text(lines)
    if not text:
        return 0
    return _count_text_tokens(text)


@functools.lru_cache(maxsize=256)
def _count_text_tokens(text: str) -> int:
    """Memoized Anthropic token count for a block of page text.

    Keyed on the exact text so identical pages/chunks reuse one count.  Kept
    separate from :func:`count_tokens` because ``lru_cache`` needs a hashable
    argument (a ``str``, not the ``list[str]`` the public seam takes).
    """
    from app.core.ai import get_ai_client  # noqa: PLC0415 - avoid import cycle at module load

    return get_ai_client().count_tokens(
        purpose=_TOKEN_COUNT_PURPOSE,
        messages=[{"role": "user", "content": text}],
    )


# ---------------------------------------------------------------------------
# Boilerplate trimming + chunking with overlap
# ---------------------------------------------------------------------------


def _line_depth(line: str) -> int:
    """Return the block-nesting depth of a rendered line from its indentation.

    The renderer indents by two spaces per depth level, so depth is the count of
    leading spaces integer-divided by two.
    """
    stripped = line.lstrip(" ")
    return (len(line) - len(stripped)) // 2


def _line_tag(line: str) -> str | None:
    """Return the tag name of a rendered line, or ``None`` if it has no ref."""
    match = _LINE_TAG_RE.match(line)
    return match.group(1) if match is not None else None


def _group_depth(lines: list[str]) -> int:
    """Return the depth at which lines are grouped into whole elements.

    This is the shallowest depth that holds more than one block, so a page whose
    reviews are wrapped in a single ``main``/``section`` is grouped at the review
    level rather than treated as one indivisible blob.  Descending past lone
    wrapper elements is what lets chunking actually split a deeply nested list
    (Requirement 1.4 / Property 5).  When every level has at most one block (a
    single straight-line chain), the minimum depth is used, yielding one group.
    """
    if not lines:
        return 0
    depths = [_line_depth(line) for line in lines]
    min_depth = min(depths)
    max_depth = max(depths)
    for depth in range(min_depth, max_depth + 1):
        if sum(1 for d in depths if d == depth) > 1:
            return depth
    return min_depth


def _group_lines(lines: list[str], group_depth: int) -> list[list[str]]:
    """Group lines into whole elements rooted at ``group_depth``.

    A new group starts at each line at ``group_depth``; shallower wrapper lines
    before the first such line, and all deeper descendant lines, travel with the
    group they belong to, so every element stays whole and no line is dropped.
    """
    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if _line_depth(line) <= group_depth and current:
            groups.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append(current)
    return groups


def _top_level_groups(lines: list[str]) -> list[list[str]]:
    """Group lines into whole elements for trimming and chunking.

    Groups at the shallowest depth that has more than one block (see
    :func:`_group_depth`), so neither trimming nor chunking ever splits a block
    across chunks (Requirement 1.4 / Property 5).
    """
    if not lines:
        return []
    return _group_lines(lines, _group_depth(lines))


def _group_has_review_cue(group: list[str]) -> bool:
    """Return ``True`` when any line in a top-level group carries a review cue."""
    for line in group:
        lowered = line.lower()
        if any(cue in lowered for cue in _CUE_SUBSTRINGS):
            return True
    return False


def _trim_boilerplate(lines: list[str]) -> list[str]:
    """Drop top-level header/footer/nav/aside blocks that hold no review cues.

    Applied only when the page is over budget (Requirement 1.4: "first drop page
    boilerplate without review cues").  Boilerplate that *does* carry a review
    cue is kept, because some sites nest the review summary in an ``aside``.
    """
    kept: list[str] = []
    for group in _top_level_groups(lines):
        tag = _line_tag(group[0])
        if tag in _BOILERPLATE_TAGS and not _group_has_review_cue(group):
            continue
        kept.extend(group)
    return kept


def chunk_lines(lines: list[str], lookup: dict[str, str], budget_tokens: int) -> list[list[str]]:
    """Split a Cleaned Page into chunks of whole elements within the budget.

    When the whole page fits ``budget_tokens``, returns it as a single chunk.
    Otherwise it first drops boilerplate without review cues, then packs the
    remaining content into chunks built from whole top-level elements, carrying
    the last :data:`_CHUNK_OVERLAP` elements of each chunk into the next so a
    review near a boundary is wholly present in at least one chunk.

    Guarantees (Property 5): every kept element appears in at least one chunk,
    and every chunk is within ``budget_tokens`` whenever that is achievable with
    whole elements.  A single element larger than the budget cannot be split
    (elements are never broken), so it forms its own over-budget chunk; this is
    rare and preferable to emitting partial elements the Locator can't resolve.

    :param lines: The rendered Cleaned Page lines.
    :param lookup: The ref-to-element lookup (unused here; kept for the stable
        seam signature and future trimming heuristics).
    :param budget_tokens: The token budget per chunk.
    :returns: The list of chunks (each a list of lines).
    """
    del lookup  # Signature kept stable; trimming works off the rendered lines.
    if not lines:
        return [[]]
    if count_tokens(lines) <= budget_tokens:
        return [list(lines)]

    trimmed = _trim_boilerplate(lines)
    if not trimmed:
        return [[]]
    if count_tokens(trimmed) <= budget_tokens:
        return [trimmed]

    groups = _top_level_groups(trimmed)
    chunks: list[list[str]] = []
    current: list[list[str]] = []  # groups accumulated into the current chunk

    def tokens_of(acc: list[list[str]]) -> int:
        return count_tokens([line for group in acc for line in group])

    def flush() -> None:
        if current:
            chunks.append([line for group in current for line in group])

    for group in groups:
        if current and tokens_of(current + [group]) > budget_tokens:
            # The current chunk is full.  Close it, then seed the next chunk
            # with the overlap tail so a review spanning the seam stays whole —
            # but never let the overlap push the new chunk over budget: trim the
            # tail until the overlap plus this group fit (budget wins over
            # overlap).  A single element larger than the budget still forms its
            # own chunk, since it can't be split.
            flush()
            overlap = current[-_CHUNK_OVERLAP:]
            while overlap and tokens_of(overlap + [group]) > budget_tokens:
                overlap = overlap[1:]
            current = overlap + [group]
        else:
            current = current + [group]
    flush()
    return chunks
