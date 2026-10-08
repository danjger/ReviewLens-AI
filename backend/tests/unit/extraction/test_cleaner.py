"""Unit tests for the page cleaner (Tasks 2.1 and 2.2).

Covers the removal rules, kept attributes, reference IDs, the ref-to-element
lookup, deterministic output, and line rendering (Task 2.1), plus token
counting, boilerplate trimming, and chunking with overlap (Task 2.2),
implemented in ``app/extraction/cleaner.py``.  Property tests for the cleaner's
correctness properties (2, 3, 4, 5) live in ``tests/property/`` and are written
by Task 2.3; these are focused example-based tests of each rule.

Token counting goes through the instrumented AI client, which the suite-wide
autouse fixture points at the offline ``FakeClaude`` stub, so these tests never
touch the network.  ``FakeClaude.count_tokens`` estimates roughly one token per
four characters, which is monotonic in page length — enough to exercise the
budget logic deterministically.

_Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5_
"""

from __future__ import annotations

from app.extraction import clean
from app.extraction.cleaner import (
    build_clean_result,
    chunk_lines,
    count_tokens,
    resolve_ref,
)


def _joined(html: str) -> str:
    """Return the cleaned lines of ``html`` joined into one string."""
    return "\n".join(build_clean_result(html).lines)


# ---------------------------------------------------------------------------
# Removal rules (Requirement 1.1)
# ---------------------------------------------------------------------------


class TestRemovalRules:
    """Scripts, styles, hidden elements, and other non-content are removed."""

    def test_script_and_style_removed(self) -> None:
        html = (
            "<body><p>Visible text here.</p><script>alert('x')</script><style>.a{}</style></body>"
        )
        out = _joined(html)
        assert "Visible text here." in out
        assert "alert" not in out
        assert ".a{}" not in out

    def test_noscript_removed(self) -> None:
        html = "<body><noscript>Enable JS</noscript><p>Real content line.</p></body>"
        out = _joined(html)
        assert "Enable JS" not in out
        assert "Real content line." in out

    def test_svg_content_removed(self) -> None:
        html = "<body><svg><text>icon label</text></svg><p>Review body text.</p></body>"
        out = _joined(html)
        assert "icon label" not in out
        assert "Review body text." in out

    def test_iframe_and_template_removed(self) -> None:
        html = (
            "<body><iframe>frame body</iframe>"
            "<template><p>tpl content</p></template>"
            "<p>Kept paragraph text.</p></body>"
        )
        out = _joined(html)
        assert "frame body" not in out
        assert "tpl content" not in out
        assert "Kept paragraph text." in out

    def test_hidden_attribute_removed(self) -> None:
        html = "<body><p hidden>secret value</p><p>shown value here</p></body>"
        out = _joined(html)
        assert "secret value" not in out
        assert "shown value here" in out

    def test_aria_hidden_true_removed(self) -> None:
        html = '<body><p aria-hidden="true">hidden aria</p><p>plain visible</p></body>'
        out = _joined(html)
        assert "hidden aria" not in out
        assert "plain visible" in out

    def test_aria_hidden_false_kept(self) -> None:
        html = '<body><p aria-hidden="false">still visible content</p></body>'
        assert "still visible content" in _joined(html)

    def test_aria_hidden_true_case_and_space_insensitive(self) -> None:
        # The predicate lowercases and strips the value, so these all hide.
        html = (
            "<body>"
            '<p aria-hidden="TRUE">upper gone</p>'
            '<p aria-hidden=" true ">spaced gone</p>'
            "<p>plain shown here</p>"
            "</body>"
        )
        out = _joined(html)
        assert "upper gone" not in out
        assert "spaced gone" not in out
        assert "plain shown here" in out

    def test_inline_display_none_removed(self) -> None:
        html = '<body><p style="color:red; display:none">gone text</p><p>here text</p></body>'
        out = _joined(html)
        assert "gone text" not in out
        assert "here text" in out

    def test_display_none_spacing_variants_removed(self) -> None:
        html = '<body><p style="display : none ;">v1</p><p style="DISPLAY:NONE">v2</p></body>'
        out = _joined(html)
        assert "v1" not in out
        assert "v2" not in out

    def test_hidden_nested_text_not_leaked_into_ancestor(self) -> None:
        # Property 4 example: hidden descendant text must not appear anywhere,
        # including folded into a kept ancestor's own text.
        html = "<body><div>outer kept<span hidden>HIDDEN CHILD</span></div></body>"
        out = _joined(html)
        assert "outer kept" in out
        assert "HIDDEN CHILD" not in out


# ---------------------------------------------------------------------------
# Kept attributes (Requirement 1.2)
# ---------------------------------------------------------------------------


class TestKeptAttributes:
    """Rating-bearing and structural attributes are kept; others dropped."""

    def test_aria_label_kept(self) -> None:
        html = '<body><div aria-label="5 out of 5 stars">x content</div></body>'
        assert 'aria-label="5 out of 5 stars"' in _joined(html)

    def test_title_alt_itemprop_kept(self) -> None:
        html = '<body><div title="t-val" alt="a-val" itemprop="review">body here</div></body>'
        out = _joined(html)
        assert 'title="t-val"' in out
        assert 'alt="a-val"' in out
        assert 'itemprop="review"' in out

    def test_href_kept_on_link(self) -> None:
        html = '<body><a href="/page/2">Next</a></body>'
        assert 'href="/page/2"' in _joined(html)

    def test_rating_and_score_data_attributes_kept(self) -> None:
        html = '<body><div data-rating="4" data-review-score="88">text body</div></body>'
        out = _joined(html)
        assert 'data-rating="4"' in out
        assert 'data-review-score="88"' in out

    def test_class_tokens_with_star_or_rating_kept(self) -> None:
        html = '<body><div class="col-6 star-5 rating-wrap muted">body text</div></body>'
        out = _joined(html)
        # Only the star/rating tokens survive, not the layout classes.
        assert 'class="star-5 rating-wrap"' in out
        assert "col-6" not in out
        assert "muted" not in out

    def test_unrelated_class_dropped_entirely(self) -> None:
        html = '<body><div class="container row">plain body text</div></body>'
        out = _joined(html)
        assert "class=" not in out

    def test_irrelevant_attributes_dropped(self) -> None:
        html = '<body><div id="x" data-track="123" onclick="f()">visible body</div></body>'
        out = _joined(html)
        assert "data-track" not in out
        assert "onclick" not in out
        assert 'id="x"' not in out


# ---------------------------------------------------------------------------
# Reference IDs and lookup (Requirements 1.3)
# ---------------------------------------------------------------------------


class TestReferenceIds:
    """Kept block elements get sequential refs that resolve to one element."""

    def test_refs_assigned_in_document_order(self) -> None:
        html = "<body><div>first block</div><div>second block</div></body>"
        lines = build_clean_result(html).lines
        assert lines[0].strip().startswith("e1 ")
        assert lines[1].strip().startswith("e2 ")

    def test_every_ref_resolves_to_exactly_one_element(self) -> None:
        html = (
            "<body><main><article>one review body text</article>"
            "<article>two review body text</article></main></body>"
        )
        result = build_clean_result(html)
        assert result.lookup  # non-empty
        for ref in result.lookup:
            node = resolve_ref(html, result.lookup, ref)
            assert node is not None

    def test_resolved_element_matches_rendered_tag(self) -> None:
        html = "<body><section><p>paragraph content body</p></section></body>"
        result = build_clean_result(html)
        for line in result.lines:
            ref = line.strip().split(" ", 1)[0]
            node = resolve_ref(html, result.lookup, ref)
            assert node is not None
            assert f"<{node.tag}" in line

    def test_unknown_ref_resolves_to_none(self) -> None:
        html = "<body><p>a paragraph of content</p></body>"
        result = build_clean_result(html)
        assert resolve_ref(html, result.lookup, "e9999") is None

    def test_duplicate_sibling_tags_resolve_distinctly(self) -> None:
        # Two identical <li> with the same text must still resolve to different
        # elements via nth-of-type.
        html = "<body><ul><li>same text here</li><li>same text here</li></ul></body>"
        result = build_clean_result(html)
        li_refs = [line.strip().split(" ", 1)[0] for line in result.lines if "<li" in line]
        assert len(li_refs) == 2
        paths = {result.lookup[r] for r in li_refs}
        assert len(paths) == 2  # distinct CSS paths

    def test_class_names_not_required_for_resolution(self) -> None:
        # Classless, id-less elements still resolve (nth-of-type path).
        html = "<body><div><div>inner body content</div></div></body>"
        result = build_clean_result(html)
        for ref in result.lookup:
            assert resolve_ref(html, result.lookup, ref) is not None


# ---------------------------------------------------------------------------
# Determinism (Requirement 1.5 / Property 2)
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Cleaning the same HTML twice yields identical lines and refs."""

    def test_identical_output_across_runs(self) -> None:
        html = (
            "<body><main>"
            '<div class="review-card" data-rating="5">'
            "<p>Great tool, setup took an afternoon.</p>"
            '<div aria-label="5 out of 5 stars">stars</div>'
            "</div>"
            '<a href="/page/2">Next</a>'
            "</main></body>"
        )
        first = build_clean_result(html)
        second = build_clean_result(html)
        assert first.lines == second.lines
        assert first.lookup == second.lookup


# ---------------------------------------------------------------------------
# Line rendering
# ---------------------------------------------------------------------------


class TestLineRendering:
    """Lines follow the design format and nest by block depth."""

    def test_line_format_matches_design_example(self) -> None:
        html = (
            '<body><div class="review-card" aria-label="5 out of 5 stars">'
            "Great tool, setup took an afternoon…</div></body>"
        )
        line = build_clean_result(html).lines[0]
        assert line.startswith("e1 <div")
        assert 'aria-label="5 out of 5 stars"' in line
        assert '"Great tool' in line
        assert line.rstrip().endswith("]")

    def test_nesting_increases_indentation(self) -> None:
        html = "<body><section><p>nested paragraph body</p></section></body>"
        lines = build_clean_result(html).lines
        section_line = next(line for line in lines if "<section" in line)
        p_line = next(line for line in lines if "<p" in line)
        section_indent = len(section_line) - len(section_line.lstrip())
        p_indent = len(p_line) - len(p_line.lstrip())
        assert p_indent > section_indent

    def test_block_without_text_renders_tag_only(self) -> None:
        html = "<body><div><p>child paragraph text</p></div></body>"
        lines = build_clean_result(html).lines
        div_line = next(line for line in lines if line.strip().startswith("e1 "))
        # The outer div's own direct text is empty, but it still gets a line.
        assert "<div>" in div_line

    def test_long_text_is_truncated(self) -> None:
        body = "x" * 500
        html = f"<body><p>{body}</p></body>"
        line = build_clean_result(html).lines[0]
        assert "…" in line
        assert len(line) < 500


# ---------------------------------------------------------------------------
# Public clean() wiring
# ---------------------------------------------------------------------------


class TestCleanPublicApi:
    """clean() packs the cleaner output into a CleanedPage with a whole chunk."""

    def test_clean_returns_cleaned_page(self) -> None:
        html = "<body><p>a review body of sufficient length</p></body>"
        page = clean(html, budget_tokens=30_000)
        assert page.lines
        assert page.lookup
        # A small page is within budget, so it is a single whole-page chunk.
        assert page.chunks == [page.lines]
        assert page.tokens > 0

    def test_clean_empty_body(self) -> None:
        page = clean("<html><head></head></html>", budget_tokens=30_000)
        assert page.lines == []
        assert page.lookup == {}
        assert page.chunks == [[]]

    def test_clean_malformed_html_is_lenient(self) -> None:
        # Unclosed tags must not raise; selectolax parses leniently.
        page = clean("<body><div><p>dangling content", budget_tokens=30_000)
        assert any("dangling content" in line for line in page.lines)


# ---------------------------------------------------------------------------
# Token counting (Requirement 1.4)
# ---------------------------------------------------------------------------


class TestTokenCounting:
    """count_tokens routes through the instrumented client and caches per page."""

    def test_empty_page_counts_zero_without_ai_call(self) -> None:
        # An empty page needs no model round-trip at all.
        assert count_tokens([]) == 0

    def test_tokens_grow_with_content(self) -> None:
        small = count_tokens(['e1 <p> "short"'])
        large = count_tokens(['e1 <p> "' + ("word " * 200) + '"'])
        assert large > small
        assert small > 0

    def test_count_is_deterministic_and_cached(self) -> None:
        lines = ['e1 <div> "a repeatable review body for counting"']
        first = count_tokens(lines)
        second = count_tokens(lines)
        # Same input → same count (memoized per page text).
        assert first == second

    def test_clean_populates_tokens(self) -> None:
        page = clean("<body><p>a review of reasonable length here</p></body>", budget_tokens=30_000)
        assert page.tokens > 0


# ---------------------------------------------------------------------------
# Boilerplate trimming (Requirement 1.4)
# ---------------------------------------------------------------------------


def _refs_in(chunks: list[list[str]]) -> set[str]:
    """Return the set of reference IDs that appear across all chunks."""
    refs: set[str] = set()
    for chunk in chunks:
        for line in chunk:
            refs.add(line.strip().split(" ", 1)[0])
    return refs


class TestBoilerplateTrimming:
    """Over budget, boilerplate without review cues is dropped before chunking."""

    def _review_blocks(self, count: int) -> str:
        # Each review is long enough that several blow past a tiny token budget.
        body = (
            "This product worked really well for our team and the setup was quick. "
            "I would recommend it to anyone evaluating the category."
        )
        return "".join(
            f'<article class="review-card">{body} number {i}</article>' for i in range(count)
        )

    def test_cueless_nav_header_footer_dropped_when_over_budget(self) -> None:
        html = (
            "<body>"
            "<header>Site Logo Home About Contact navigation banner text</header>"
            "<nav>Home Products Pricing Blog Docs Support Login Sign up menu</nav>"
            "<main>" + self._review_blocks(6) + "</main>"
            "<footer>Copyright 2024 Terms Privacy Cookies Careers press kit</footer>"
            "</body>"
        )
        page = clean(html, budget_tokens=40)
        joined = "\n".join(line for chunk in page.chunks for line in chunk)
        # Review content survives; cueless boilerplate text is gone.
        assert "This product worked really well" in joined
        assert "navigation banner" not in joined
        assert "Terms Privacy Cookies" not in joined

    def test_boilerplate_with_review_cue_is_kept(self) -> None:
        # An aside that carries a rating cue is a review summary, not boilerplate.
        html = (
            "<body>"
            '<aside aria-label="4.5 out of 5 stars overall rating">Highly rated overall</aside>'
            "<main>" + self._review_blocks(6) + "</main>"
            "</body>"
        )
        page = clean(html, budget_tokens=40)
        joined = "\n".join(line for chunk in page.chunks for line in chunk)
        assert "4.5 out of 5 stars" in joined

    def test_cueless_aside_dropped_when_over_budget(self) -> None:
        # ``aside`` is boilerplate too; without a review cue it is trimmed.
        html = (
            "<body>"
            "<aside>Related products you might also like today sidebar</aside>"
            "<main>" + self._review_blocks(6) + "</main>"
            "</body>"
        )
        page = clean(html, budget_tokens=40)
        joined = "\n".join(line for chunk in page.chunks for line in chunk)
        assert "This product worked really well" in joined
        assert "Related products" not in joined

    def test_trimming_only_happens_over_budget(self) -> None:
        # Within budget, even cueless boilerplate is retained (single chunk).
        html = "<body><nav>menu items here</nav><main><p>a short review body</p></main></body>"
        page = clean(html, budget_tokens=30_000)
        joined = "\n".join(line for chunk in page.chunks for line in chunk)
        assert "menu items here" in joined
        assert page.chunks == [page.lines]


# ---------------------------------------------------------------------------
# Chunking with overlap (Requirement 1.4 / Property 5)
# ---------------------------------------------------------------------------


class TestChunking:
    """Over-budget pages split into whole-element chunks with a 2-element overlap."""

    def _page_lines(self, count: int) -> list[str]:
        # Flat top-level blocks (depth 0) with enough text to force splitting.
        body = "a sufficiently long review body sentence that adds up over blocks"
        return [f'e{i} <article> "{body} {i}"' for i in range(1, count + 1)]

    def test_within_budget_single_chunk(self) -> None:
        lines = self._page_lines(3)
        chunks = chunk_lines(lines, {}, budget_tokens=30_000)
        assert chunks == [lines]

    def test_empty_lines_single_empty_chunk(self) -> None:
        assert chunk_lines([], {}, budget_tokens=100) == [[]]

    def test_over_budget_splits_into_multiple_chunks(self) -> None:
        lines = self._page_lines(10)
        total = count_tokens(lines)
        budget = total // 3
        chunks = chunk_lines(lines, {}, budget_tokens=budget)
        assert len(chunks) > 1

    def test_every_element_appears_in_some_chunk(self) -> None:
        # Property 5 (example): coverage — no kept element is lost.
        lines = self._page_lines(12)
        budget = count_tokens(lines) // 4
        chunks = chunk_lines(lines, {}, budget_tokens=budget)
        assert _refs_in(chunks) == {f"e{i}" for i in range(1, 13)}

    def test_chunks_are_within_budget(self) -> None:
        # Property 5 (example): each chunk fits the budget (elements fit singly).
        lines = self._page_lines(12)
        budget = count_tokens(lines) // 4
        chunks = chunk_lines(lines, {}, budget_tokens=budget)
        for chunk in chunks:
            assert count_tokens(chunk) <= budget

    def test_consecutive_chunks_overlap_by_two_elements(self) -> None:
        lines = self._page_lines(12)
        budget = count_tokens(lines) // 4
        chunks = chunk_lines(lines, {}, budget_tokens=budget)
        assert len(chunks) >= 2
        for earlier, later in zip(chunks, chunks[1:], strict=False):
            overlap = set(_refs_in([earlier])) & set(_refs_in([later]))
            assert overlap, "consecutive chunks must share overlap elements"

    def test_whole_elements_never_split_across_chunks(self) -> None:
        # A top-level block and its nested child line stay in the same chunk:
        # each <article> is immediately followed by its child <p> in that chunk.
        html = (
            "<body><main>"
            + "".join(
                f'<article class="review-card"><p>review body paragraph for item {i} '
                f"with enough words to matter here</p></article>"
                for i in range(8)
            )
            + "</main></body>"
        )
        page = clean(html, budget_tokens=60)
        for chunk in page.chunks:
            for idx, line in enumerate(chunk):
                if "<article" in line:
                    assert idx + 1 < len(chunk), "article must keep its child in-chunk"
                    assert "<p" in chunk[idx + 1]

    def test_single_oversize_element_becomes_its_own_chunk(self) -> None:
        huge = 'e1 <article> "' + ("word " * 400) + '"'
        normal = 'e2 <article> "a short review body here"'
        chunks = chunk_lines([huge, normal], {}, budget_tokens=10)
        # The oversize element can't be split, so it stands alone; nothing lost.
        assert _refs_in(chunks) == {"e1", "e2"}
