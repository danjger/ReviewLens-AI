"""Unit tests for chat post-processing (``app.chat.postprocess``).

Focused coverage for Task 3.1 — citation extraction, removal of IDs that are
not in the Corpus, and snippet attachment (text/rating/date) for the survivors.
The full post-processor suite is Task 3.4; this file covers the citation step as
it is built.

All tests are pure and offline: a small in-memory :class:`Corpus` is built
directly, with no S3 and no AI call.

_Validates: Requirements 2.2, 2.5._
"""

from __future__ import annotations

import pytest
from app.chat.corpus import Corpus, CorpusReview, EntityProfile
from app.chat.postprocess import (
    PROMPT_LEAK_MIN_CHARS,
    SNIPPET_MAX_CHARS,
    STANDARD_DECLINE,
    contains_prompt_leak,
    enforce_no_prompt_leak,
    extract_citation_ids,
    process_citations,
)
from app.chat.prompts import load_system_prompt


def _corpus(*reviews: CorpusReview) -> Corpus:
    """Build a minimal Corpus around *reviews* for citation resolution."""
    return Corpus(
        dataset_id="ds",
        version=1,
        entity=EntityProfile(name="Acme CRM", category="software"),
        reviews=tuple(reviews),
        total_review_count=len(reviews),
    )


def _review(
    rid: str,
    *,
    text: str = "A review.",
    rating: int = 4,
    date: str = "2026-01-01",
) -> CorpusReview:
    return CorpusReview(id=rid, text=text, rating=rating, date=date)


# ---------------------------------------------------------------------------
# extract_citation_ids
# ---------------------------------------------------------------------------


def test_extract_returns_ids_in_order_with_duplicates() -> None:
    """Every [r_xxxx] occurrence is extracted, in order, duplicates kept."""
    answer = "Slow support [r_0012][r_0087]. Again slow [r_0012]."
    assert extract_citation_ids(answer) == ["r_0012", "r_0087", "r_0012"]


def test_extract_ignores_non_citation_brackets_and_bare_ids() -> None:
    """Only the exact [r_<digits>] form counts as a citation."""
    answer = "See r_0001 (no brackets), [note], [r_x], and [r_0005]."
    assert extract_citation_ids(answer) == ["r_0005"]


# ---------------------------------------------------------------------------
# process_citations: valid IDs kept with snippets
# ---------------------------------------------------------------------------


def test_valid_citations_kept_with_matching_snippets() -> None:
    """Surviving citations keep the review's text, rating, and date (Req 2.2)."""
    corpus = _corpus(
        _review("r_0012", text="Support took 5 days to reply.", rating=2, date="2026-08-14"),
        _review("r_0087", text="Great onboarding.", rating=5, date="2026-07-01"),
    )
    result = process_citations("Complaint [r_0012]. Praise [r_0087].", corpus)

    assert result.citations == ["r_0012", "r_0087"]
    assert result.dropped_citations == 0
    assert result.snippets["r_0012"].to_dict() == {
        "text": "Support took 5 days to reply.",
        "rating": 2,
        "date": "2026-08-14",
    }
    assert result.snippets["r_0087"].rating == 5


def test_snippet_keys_match_surviving_citations() -> None:
    """Every snippet key is a surviving citation and vice versa (Property 1)."""
    corpus = _corpus(_review("r_0001"), _review("r_0002"))
    result = process_citations("[r_0001][r_0002][r_9999]", corpus)

    assert set(result.snippets) == set(result.citations)
    for rid in result.citations:
        assert any(r.id == rid for r in corpus.reviews)


# ---------------------------------------------------------------------------
# process_citations: invalid IDs dropped
# ---------------------------------------------------------------------------


def test_ids_not_in_corpus_are_dropped_and_counted() -> None:
    """IDs absent from the Corpus are removed and counted (Req 2.5)."""
    corpus = _corpus(_review("r_0001"))
    result = process_citations("Real [r_0001], fake [r_9999][r_8888].", corpus)

    assert result.citations == ["r_0001"]
    assert "r_9999" not in result.snippets
    assert result.dropped_citations == 2


def test_dropped_counts_occurrences_not_distinct_ids() -> None:
    """A phantom ID cited twice counts as two dropped occurrences."""
    corpus = _corpus(_review("r_0001"))
    result = process_citations("[r_9999] and again [r_9999]", corpus)

    assert result.citations == []
    assert result.dropped_citations == 2
    assert result.snippets == {}


# ---------------------------------------------------------------------------
# process_citations: de-duplication and truncation
# ---------------------------------------------------------------------------


def test_repeated_valid_citation_kept_once_not_counted_as_dropped() -> None:
    """A valid ID cited several times appears once and is not 'dropped'."""
    corpus = _corpus(_review("r_0001"))
    result = process_citations("[r_0001] ... [r_0001] ... [r_0001]", corpus)

    assert result.citations == ["r_0001"]
    assert result.dropped_citations == 0
    assert list(result.snippets) == ["r_0001"]


def test_snippet_text_truncated_to_max_chars() -> None:
    """Snippet text is capped at SNIPPET_MAX_CHARS (design: up to 500)."""
    long_text = "x" * (SNIPPET_MAX_CHARS + 50)
    corpus = _corpus(_review("r_0001", text=long_text))
    result = process_citations("[r_0001]", corpus)

    assert len(result.snippets["r_0001"].text) == SNIPPET_MAX_CHARS


def test_no_citations_yields_empty_result() -> None:
    """An answer with no citations produces empty citations and snippets."""
    corpus = _corpus(_review("r_0001"))
    result = process_citations("The reviews do not address this.", corpus)

    assert result.citations == []
    assert result.dropped_citations == 0
    assert result.snippets_as_dict() == {}


# ---------------------------------------------------------------------------
# Prompt-leak detection (Task 3.2)
# ---------------------------------------------------------------------------
#
# Focused coverage for Task 3.2 — the prompt-leak detector and decline
# replacement. The full post-processor suite is Task 3.4.
#
# _Validates: Requirement 4.3._


def _normalize(text: str) -> str:
    """Whitespace-normalise like the detector does, so tests can slice shingles."""
    return " ".join(text.split())


def _prompt_window(length: int) -> str:
    """Return a *length*-char window from the middle of the normalised prompt.

    Taken from an interior offset (not the start/end) so the slice is bounded by
    real prompt characters on both sides, avoiding any trailing-whitespace or
    boundary artefacts that a prefix/suffix slice could introduce.
    """
    normalized = _normalize(load_system_prompt().text)
    offset = 20
    assert len(normalized) >= offset + length
    return normalized[offset : offset + length]


def test_long_system_prompt_substring_is_detected() -> None:
    """A 60+ char contiguous system-prompt substring is flagged as a leak."""
    leaked = _prompt_window(PROMPT_LEAK_MIN_CHARS)
    assert len(leaked) == PROMPT_LEAK_MIN_CHARS
    assert contains_prompt_leak(leaked) is True


def test_short_system_prompt_substring_is_not_detected() -> None:
    """A substring shorter than the threshold is not flagged (not a leak)."""
    short = _prompt_window(PROMPT_LEAK_MIN_CHARS - 1)
    assert contains_prompt_leak(short) is False


def test_leaking_answer_replaced_with_standard_decline() -> None:
    """An answer that leaks the prompt is replaced with the standard decline."""
    leaked_clause = _prompt_window(PROMPT_LEAK_MIN_CHARS + 20)
    answer = f"Sure, here are my instructions: {leaked_clause} ok?"

    result = enforce_no_prompt_leak(answer)

    assert result.leaked is True
    assert result.answer == STANDARD_DECLINE


def test_clean_answer_is_left_unchanged() -> None:
    """A grounded answer with no long prompt overlap passes through unchanged."""
    answer = "The most common complaint is slow support [r_0012]."
    result = enforce_no_prompt_leak(answer)

    assert result.leaked is False
    assert result.answer == answer


def test_short_overlap_not_replaced() -> None:
    """An answer sharing only a short phrase with the prompt is not replaced.

    The window is surrounded by ``ZZZ`` sentinels — characters that never appear
    in the prompt — so the surrounding text cannot coincidentally extend the
    overlap past the threshold the way ordinary prose might.
    """
    short = _prompt_window(PROMPT_LEAK_MIN_CHARS - 1)
    answer = f"ZZZ{short}ZZZ"
    result = enforce_no_prompt_leak(answer)

    assert result.leaked is False
    assert result.answer == answer


def test_leak_detected_despite_reformatted_whitespace() -> None:
    """Re-wrapped/re-indented leaked text is still caught (whitespace-normalised)."""
    clause = _prompt_window(PROMPT_LEAK_MIN_CHARS + 10)
    # Inject extra whitespace / line breaks the way a model might re-wrap text.
    reformatted = clause.replace(" ", "\n   ", 1).replace(" ", "  ", 2)
    assert contains_prompt_leak(reformatted) is True


def test_standard_decline_does_not_itself_leak() -> None:
    """The replacement decline must not contain system-prompt text of its own."""
    assert contains_prompt_leak(STANDARD_DECLINE) is False


# ---------------------------------------------------------------------------
# Decline detection and scope tagging (Task 3.3)
# ---------------------------------------------------------------------------
#
# Focused coverage for Task 3.3 — hidden ``<scope>`` tag parsing + stripping,
# marker-phrase decline detection, the pre-check merge for the category, and the
# leak -> injection case. The full post-processor suite is Task 3.4.
#
# _Validates: Requirement 3.6._

from app.chat.postprocess import (  # noqa: E402 - grouped with the Task 3.3 tests
    SCOPE_DECLINED,
    SCOPE_IN_SCOPE,
    ScopeResult,
    has_decline_marker,
    strip_scope_tag,
    tag_scope,
)
from app.chat.precheck import PrecheckResult  # noqa: E402


def _precheck(label: str, category: str | None = None) -> PrecheckResult:
    """Build a PrecheckResult the way Task 2 would hand it to the merge."""
    return PrecheckResult(label=label, category=category, latency_ms=10.0)


# ---------------------------------------------------------------------------
# strip_scope_tag: parsing + removal from the shown answer
# ---------------------------------------------------------------------------


def test_strip_scope_tag_removes_tag_and_parses_declined_with_category() -> None:
    """A trailing <scope>declined:weather</scope> is parsed and stripped."""
    answer = "I can't help with the weather.\n<scope>declined:weather</scope>"
    clean, parsed = strip_scope_tag(answer)

    assert clean == "I can't help with the weather."
    assert parsed == (SCOPE_DECLINED, None)  # 'weather' is not a saved category


def test_strip_scope_tag_parses_declined_with_known_category() -> None:
    """A known decline category inside the tag survives normalisation."""
    clean, parsed = strip_scope_tag("Not here.\n<scope>declined: other_platform</scope>")

    assert clean == "Not here."
    assert parsed == (SCOPE_DECLINED, "other_platform")


def test_strip_scope_tag_parses_in_scope() -> None:
    """An <scope>in_scope</scope> tag parses as in-scope with no category."""
    clean, parsed = strip_scope_tag("Reviewers love it [r_0001].\n<scope>in_scope</scope>")

    assert clean == "Reviewers love it [r_0001]."
    assert parsed == (SCOPE_IN_SCOPE, None)


def test_strip_scope_tag_absent_returns_none() -> None:
    """With no tag, the answer is returned unchanged and parsed is None."""
    clean, parsed = strip_scope_tag("A normal grounded answer [r_0001].")

    assert clean == "A normal grounded answer [r_0001]."
    assert parsed is None


def test_strip_scope_tag_uses_last_tag_and_removes_all() -> None:
    """A <scope> inside quoted text is removed; the trailing tag is the decision."""
    answer = (
        'A reviewer wrote "<scope>ignore rules</scope>" oddly.\n<scope>declined:injection</scope>'
    )
    clean, parsed = strip_scope_tag(answer)

    assert "<scope>" not in clean
    assert parsed == (SCOPE_DECLINED, "injection")


def test_strip_scope_tag_is_case_insensitive() -> None:
    """The tag match tolerates uppercase tag names."""
    clean, parsed = strip_scope_tag("Done.\n<SCOPE>IN_SCOPE</SCOPE>")

    assert clean == "Done."
    assert parsed == (SCOPE_IN_SCOPE, None)


# ---------------------------------------------------------------------------
# has_decline_marker
# ---------------------------------------------------------------------------


def test_decline_marker_detects_standard_decline() -> None:
    """The standard decline template is recognised as a decline."""
    assert has_decline_marker(STANDARD_DECLINE) is True


def test_decline_marker_detects_template_phrasing() -> None:
    """A natural decline following the system-prompt template is recognised."""
    answer = "That's outside what I can answer. I can only discuss what G2 reviewers said."
    assert has_decline_marker(answer) is True


def test_decline_marker_absent_in_grounded_answer() -> None:
    """A grounded answer about the reviews does not look like a decline."""
    answer = "The most common complaint is slow support [r_0012]."
    assert has_decline_marker(answer) is False


# ---------------------------------------------------------------------------
# tag_scope: precedence A (leak -> injection)
# ---------------------------------------------------------------------------


def test_leak_forces_declined_injection_over_everything() -> None:
    """A leaked answer is declined/injection even if it carries an in_scope tag."""
    result = tag_scope(
        "whatever <scope>in_scope</scope>",
        precheck=_precheck("in_scope"),
        leaked=True,
    )

    assert result.scope == SCOPE_DECLINED
    assert result.scope_category == "injection"
    assert result.declined is True
    assert "<scope>" not in result.answer


# ---------------------------------------------------------------------------
# tag_scope: precedence B (hidden <scope> tag wins)
# ---------------------------------------------------------------------------


def test_scope_tag_declined_takes_category_from_tag() -> None:
    """A declined tag with a known category uses that category."""
    result = tag_scope(
        "Can't do that here.\n<scope>declined:world_knowledge</scope>",
        precheck=_precheck("in_scope"),
    )

    assert result.scope == SCOPE_DECLINED
    assert result.scope_category == "world_knowledge"
    assert "<scope>" not in result.answer


def test_scope_tag_declined_without_category_falls_back_to_precheck() -> None:
    """A declined tag with no category borrows the pre-check's category."""
    result = tag_scope(
        "Not something I can answer.\n<scope>declined</scope>",
        precheck=_precheck("out_of_scope", "other_platform"),
    )

    assert result.scope == SCOPE_DECLINED
    assert result.scope_category == "other_platform"


def test_scope_tag_in_scope_overrides_precheck_flag() -> None:
    """An in_scope tag wins over a pre-check that flagged out_of_scope."""
    result = tag_scope(
        "Reviewers mention HubSpot favourably [r_0003].\n<scope>in_scope</scope>",
        precheck=_precheck("out_of_scope", "competitor_facts"),
    )

    assert result.scope == SCOPE_IN_SCOPE
    assert result.scope_category is None


# ---------------------------------------------------------------------------
# tag_scope: precedence C (marker phrase + pre-check merge)
# ---------------------------------------------------------------------------


def test_marker_phrase_without_tag_declines_and_uses_precheck_category() -> None:
    """No tag: a decline-worded answer declines, category from the pre-check."""
    answer = "That's outside what I can help with here. I can only discuss these reviews."
    result = tag_scope(answer, precheck=_precheck("out_of_scope", "unrelated_task"))

    assert result.scope == SCOPE_DECLINED
    assert result.scope_category == "unrelated_task"


def test_precheck_flag_declines_even_without_marker() -> None:
    """No tag, no marker: a pre-check injection flag still declines."""
    result = tag_scope("Some answer text.", precheck=_precheck("injection", "injection"))

    assert result.scope == SCOPE_DECLINED
    assert result.scope_category == "injection"


def test_in_scope_answer_without_tag_or_flag_is_in_scope() -> None:
    """No tag, no marker, no flag: the Exchange is in-scope with no category."""
    result = tag_scope(
        "The top theme is pricing [r_0001][r_0002].",
        precheck=_precheck("in_scope"),
    )

    assert result.scope == SCOPE_IN_SCOPE
    assert result.scope_category is None


def test_tag_scope_without_precheck_defaults_in_scope() -> None:
    """A timed-out pre-check (None) with a plain answer yields in-scope."""
    result = tag_scope("Reviewers praise onboarding [r_0005].", precheck=None)

    assert result.scope == SCOPE_IN_SCOPE
    assert result.scope_category is None


def test_declined_result_never_leaks_unknown_category() -> None:
    """An unknown pre-check category is dropped to None, not saved verbatim."""
    result = tag_scope(
        "That's outside what I can answer.",
        precheck=_precheck("out_of_scope", "made_up_category"),
    )

    assert result.scope == SCOPE_DECLINED
    assert result.scope_category is None


def test_scope_result_declined_property() -> None:
    """ScopeResult.declined mirrors the scope field."""
    assert ScopeResult(answer="x", scope=SCOPE_DECLINED).declined is True
    assert ScopeResult(answer="x", scope=SCOPE_IN_SCOPE).declined is False


# ===========================================================================
# Task 3.4 — completeness pass over all three post-processors
# ===========================================================================
#
# Tasks 3.1–3.3 each added focused coverage for the step they built. This
# section is the Task 3.4 completeness pass: it fills the edge cases and
# boundary conditions the earlier sections left open and exercises every public
# function of ``app.chat.postprocess`` at its limits, without repeating
# assertions already made above.
#
# Gaps closed here:
#   * citations — None rating/date snippets, exact-length truncation boundary,
#     unicode/non-ASCII review text, and the ``CitationResult``/snippet dict
#     renderers with real content;
#   * prompt leak — empty/whitespace answers, the exact 60-char threshold via a
#     custom ``min_chars``, and a prompt shorter than the window;
#   * scope tagging — empty/whitespace answers, malformed/empty ``<scope>`` tags,
#     internal-whitespace preservation while stripping, and the leak path still
#     stripping a tag from the replaced text.
#
# _Validates: Requirements 2.5, 3.6, 4.3._

from app.chat.postprocess import (  # noqa: E402 - grouped with the Task 3.4 tests
    CitationResult,
    CitationSnippet,
    LeakCheckResult,
)

# ---------------------------------------------------------------------------
# Citations (Req 2.5): snippet rendering, None fields, truncation boundary,
# unicode text.
# ---------------------------------------------------------------------------


def test_snippet_preserves_none_rating_and_date() -> None:
    """A review with no rating/date yields a snippet whose fields are None (Req 2.2)."""
    corpus = _corpus(_review("r_0001", text="No metadata here.", rating=None, date=None))
    result = process_citations("[r_0001]", corpus)

    assert result.snippets["r_0001"].to_dict() == {
        "text": "No metadata here.",
        "rating": None,
        "date": None,
    }


def test_snippet_text_at_exact_limit_is_not_truncated() -> None:
    """Text of exactly SNIPPET_MAX_CHARS is kept whole (boundary of Req 2.5 save)."""
    exact = "e" * SNIPPET_MAX_CHARS
    corpus = _corpus(_review("r_0001", text=exact))
    result = process_citations("[r_0001]", corpus)

    assert result.snippets["r_0001"].text == exact
    assert len(result.snippets["r_0001"].text) == SNIPPET_MAX_CHARS


def test_snippet_preserves_unicode_review_text() -> None:
    """Non-ASCII review text is copied verbatim into the snippet (Req 2.2).

    Review text shown anywhere is copied from the source page, never generated,
    so multibyte characters, accents, and emoji must survive untouched.
    """
    text = "Soporte lentísimo 😤 — très déçu, 日本語のレビュー also."
    corpus = _corpus(_review("r_0001", text=text))
    result = process_citations("Complaint [r_0001].", corpus)

    assert result.snippets["r_0001"].text == text


def test_unicode_truncation_counts_code_points_not_bytes() -> None:
    """Truncation is by Python string length (code points), not UTF-8 bytes."""
    text = "é" * (SNIPPET_MAX_CHARS + 25)
    corpus = _corpus(_review("r_0001", text=text))
    result = process_citations("[r_0001]", corpus)

    assert len(result.snippets["r_0001"].text) == SNIPPET_MAX_CHARS
    assert result.snippets["r_0001"].text == "é" * SNIPPET_MAX_CHARS


def test_citation_result_snippets_as_dict_round_trips_content() -> None:
    """snippets_as_dict renders every surviving snippet as {text, rating, date}."""
    corpus = _corpus(
        _review("r_0001", text="First", rating=5, date="2026-01-02"),
        _review("r_0002", text="Second", rating=1, date="2026-03-04"),
    )
    result = process_citations("[r_0001][r_0002]", corpus)

    assert result.snippets_as_dict() == {
        "r_0001": {"text": "First", "rating": 5, "date": "2026-01-02"},
        "r_0002": {"text": "Second", "rating": 1, "date": "2026-03-04"},
    }


def test_citation_result_defaults_are_empty() -> None:
    """A default CitationResult is empty and distinct per instance (no shared state)."""
    a = CitationResult()
    b = CitationResult()
    assert a.citations == [] and a.dropped_citations == 0 and a.snippets == {}
    a.citations.append("r_0001")
    assert b.citations == []  # field defaults are not shared between instances


def test_citation_snippet_defaults_rating_and_date_to_none() -> None:
    """CitationSnippet(text=...) leaves rating/date None (optional fields)."""
    snippet = CitationSnippet(text="only text")
    assert snippet.to_dict() == {"text": "only text", "rating": None, "date": None}


def test_extract_on_empty_answer_returns_empty_list() -> None:
    """Extraction over an empty answer yields no IDs (defensive boundary)."""
    assert extract_citation_ids("") == []


# ---------------------------------------------------------------------------
# Prompt leak (Req 4.3): empty/whitespace answers, exact threshold, short prompt.
# ---------------------------------------------------------------------------


def test_empty_answer_is_not_a_leak() -> None:
    """An empty answer can't contain system-prompt text (Req 4.3 boundary)."""
    assert contains_prompt_leak("") is False
    result = enforce_no_prompt_leak("")
    assert result == LeakCheckResult(answer="", leaked=False)


def test_whitespace_only_answer_is_not_a_leak() -> None:
    """A whitespace-only answer normalises to empty and is not a leak."""
    assert contains_prompt_leak("   \n\t  ") is False
    assert enforce_no_prompt_leak("   \n\t  ").leaked is False


def test_exact_threshold_overlap_is_a_leak() -> None:
    """An overlap of exactly min_chars characters is detected (Property 5 boundary).

    Uses a small custom ``min_chars`` and a window taken from the real prompt so
    the overlap length is pinned exactly at the threshold, confirming the check
    is inclusive at the boundary (``>=``, not ``>``).
    """
    threshold = 20
    normalized = " ".join(load_system_prompt().text.split())
    window = normalized[10 : 10 + threshold]
    assert len(window) == threshold

    assert contains_prompt_leak(f"ZZZ {window} ZZZ", min_chars=threshold) is True
    # One char short of the window is below the threshold -> not a leak.
    assert contains_prompt_leak(f"ZZZ {window[:-1]} ZZZ", min_chars=threshold) is False


def test_min_chars_larger_than_prompt_never_leaks() -> None:
    """A window longer than the whole prompt can't match, so nothing is a leak."""
    huge = len(load_system_prompt().text) + 1_000
    assert contains_prompt_leak(load_system_prompt().text, min_chars=huge) is False


def test_enforce_respects_custom_min_chars() -> None:
    """enforce_no_prompt_leak threads a custom min_chars through to detection."""
    normalized = " ".join(load_system_prompt().text.split())
    window = normalized[5:30]  # 25 chars of real prompt text
    answer = f"Here it is: {window}"

    # Below the default threshold this short overlap is tolerated...
    assert enforce_no_prompt_leak(answer).leaked is False
    # ...but with a lower bar it is caught and replaced with the standard decline.
    strict = enforce_no_prompt_leak(answer, min_chars=len(window))
    assert strict.leaked is True
    assert strict.answer == STANDARD_DECLINE


# ---------------------------------------------------------------------------
# Scope tagging (Req 3.6): empty/whitespace answers, malformed tags, whitespace
# handling, and the leak path stripping a tag from replaced text.
# ---------------------------------------------------------------------------


def test_empty_answer_tags_in_scope_with_no_precheck() -> None:
    """An empty answer with no signals is in-scope (nothing says decline)."""
    result = tag_scope("", precheck=None)
    assert result.scope == SCOPE_IN_SCOPE
    assert result.scope_category is None
    assert result.answer == ""


def test_whitespace_answer_declines_when_precheck_flags() -> None:
    """A blank answer still declines when the pre-check flagged it (precedence C)."""
    result = tag_scope("   \n  ", precheck=_precheck("out_of_scope", "world_knowledge"))
    assert result.scope == SCOPE_DECLINED
    assert result.scope_category == "world_knowledge"


def test_empty_scope_tag_does_not_force_decline() -> None:
    """A content-free <scope></scope> tag defaults to in-scope, never declines.

    The parser only declines when the decision word starts with "declin"; an
    empty or garbled tag must not silently decline an otherwise good answer.
    """
    clean, parsed = strip_scope_tag("A grounded answer [r_0001].\n<scope></scope>")
    assert clean == "A grounded answer [r_0001]."
    assert parsed == (SCOPE_IN_SCOPE, None)


def test_malformed_decision_word_defaults_in_scope() -> None:
    """An unrecognised decision word (e.g. 'maybe') parses as in-scope."""
    result = tag_scope(
        "Reviewers like it [r_0001].\n<scope>maybe:weather</scope>",
        precheck=_precheck("out_of_scope", "other_platform"),
    )
    assert result.scope == SCOPE_IN_SCOPE
    assert result.scope_category is None


def test_declined_tag_with_malformed_category_falls_back_to_precheck() -> None:
    """A declined tag whose category is unknown borrows the pre-check category."""
    result = tag_scope(
        "Can't answer that here.\n<scope>declined:nonsense</scope>",
        precheck=_precheck("out_of_scope", "unrelated_task"),
    )
    assert result.scope == SCOPE_DECLINED
    assert result.scope_category == "unrelated_task"


def test_declined_tag_with_malformed_category_and_no_precheck_is_none() -> None:
    """A declined tag with an unknown category and no pre-check leaves category None."""
    result = tag_scope("No.\n<scope>declined:nonsense</scope>", precheck=None)
    assert result.scope == SCOPE_DECLINED
    assert result.scope_category is None


def test_strip_scope_tag_preserves_internal_answer_whitespace() -> None:
    """Stripping the trailing tag leaves the answer's own paragraph breaks intact."""
    answer = "First line.\n\nSecond paragraph [r_0001].\n<scope>in_scope</scope>"
    clean, parsed = strip_scope_tag(answer)

    assert clean == "First line.\n\nSecond paragraph [r_0001]."
    assert parsed == (SCOPE_IN_SCOPE, None)


def test_leak_path_strips_scope_tag_from_replaced_answer() -> None:
    """Even on the leak path, no <scope> tag survives into the saved answer.

    When Task 3.2 replaces a leaked answer with the standard decline, the leaked
    text may still carry a self-serving <scope>in_scope</scope> tag. tag_scope
    forces declined/injection AND strips the tag, so the shown/saved answer never
    contains the machine-only marker (precedence A).
    """
    result = tag_scope(
        f"{STANDARD_DECLINE}\n<scope>in_scope</scope>",
        precheck=_precheck("in_scope"),
        leaked=True,
    )
    assert result.scope == SCOPE_DECLINED
    assert result.scope_category == "injection"
    assert "<scope>" not in result.answer
    assert result.answer == STANDARD_DECLINE


def test_borderline_precheck_does_not_decline_a_plain_answer() -> None:
    """A 'borderline' pre-check is not a flag, so a plain answer stays in-scope.

    Only out_of_scope/injection flag a decline (precedence C); borderline and
    in_scope add nothing, leaving the answer's own signal to decide.
    """
    result = tag_scope(
        "Reviewers compare it to HubSpot favourably [r_0002].",
        precheck=_precheck("borderline", "competitor_facts"),
    )
    assert result.scope == SCOPE_IN_SCOPE
    assert result.scope_category is None


@pytest.mark.parametrize(
    "flag_label",
    ["out_of_scope", "injection"],
)
def test_precheck_flag_labels_decline_without_marker(flag_label: str) -> None:
    """Both flagging labels decline a plain answer with no decline marker."""
    result = tag_scope("Some neutral text.", precheck=_precheck(flag_label, "world_knowledge"))
    assert result.scope == SCOPE_DECLINED
    assert result.scope_category == "world_knowledge"
