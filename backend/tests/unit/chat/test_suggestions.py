"""Unit tests for starter-question suggestions (``app.chat.suggestions``), Task 5.3.

Covers the design's "Endpoints" rule and Requirement 1.4: 3–4 starter questions
built from theme labels using templates (no AI call), with general fallback
questions when no themes are available.

- **Template**: a theme label becomes "What do reviewers say about {label}?",
  including the Requirement 1.4 example ("customer support").
- **Cap and ordering**: at most 4 questions, from the top themes by ``mentions``.
- **Dedup**: labels that differ only by case/space don't produce duplicates.
- **Fallback**: the general questions are returned when there are no themes, no
  active version (``metrics is None``), or no usable labels; a short theme set is
  topped up to the minimum with distinct fallbacks.

The themes read (:func:`app.chat.suggestions._active_themes`, which reads
``Dataset.metrics["themes"]`` via ``session_scope``) is faked at the module
boundary — integration against LocalStack is Task 5.4.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.chat import suggestions
from app.chat.suggestions import (
    FALLBACK_QUESTIONS,
    MAX_SUGGESTIONS,
    MIN_SUGGESTIONS,
    build_suggestions,
    themes_to_questions,
)

_DS = "11111111-1111-1111-1111-111111111111"


def _theme(label: str, mentions: int = 1, lean: str = "neutral") -> dict[str, Any]:
    """A ``metrics.themes`` entry in the design's ``{label, mentions, lean}`` shape."""
    return {"label": label, "mentions": mentions, "lean": lean}


# ---------------------------------------------------------------------------
# themes_to_questions: template, ordering, cap, dedup
# ---------------------------------------------------------------------------


def test_theme_label_uses_requirement_template() -> None:
    """The Requirement 1.4 example: "customer support" → the templated question."""
    questions = themes_to_questions([_theme("customer support")])
    assert questions == ["What do reviewers say about customer support?"]


def test_questions_ordered_by_mentions_descending() -> None:
    """More-mentioned themes come first, so the top topics win under the cap."""
    themes = [
        _theme("delivery", mentions=5),
        _theme("price", mentions=20),
        _theme("support", mentions=12),
    ]
    questions = themes_to_questions(themes)
    assert questions == [
        "What do reviewers say about price?",
        "What do reviewers say about support?",
        "What do reviewers say about delivery?",
    ]


def test_cap_keeps_only_top_four_themes() -> None:
    """With more than 4 themes, only the 4 most-mentioned become questions."""
    themes = [_theme(f"topic{i}", mentions=i) for i in range(1, 8)]  # topic1..topic7
    questions = themes_to_questions(themes)
    assert len(questions) == MAX_SUGGESTIONS
    # Highest mentions are topic7..topic4.
    assert questions == [
        "What do reviewers say about topic7?",
        "What do reviewers say about topic6?",
        "What do reviewers say about topic5?",
        "What do reviewers say about topic4?",
    ]


def test_ties_preserve_input_order() -> None:
    """Equal ``mentions`` keep the themes' input order (stable sort)."""
    themes = [_theme("alpha", mentions=3), _theme("beta", mentions=3)]
    questions = themes_to_questions(themes)
    assert questions == [
        "What do reviewers say about alpha?",
        "What do reviewers say about beta?",
    ]


def test_dedup_case_and_space_insensitive() -> None:
    """Labels differing only by case/space don't produce duplicate questions."""
    themes = [
        _theme("Customer Support", mentions=10),
        _theme("customer support", mentions=8),
        _theme("  Customer Support  ", mentions=6),
        _theme("Billing", mentions=4),
    ]
    questions = themes_to_questions(themes)
    assert questions == [
        "What do reviewers say about Customer Support?",
        "What do reviewers say about Billing?",
    ]


def test_blank_or_malformed_labels_skipped() -> None:
    """Entries with no usable label are skipped rather than yielding empty questions."""
    themes = [
        {"mentions": 5},  # no label
        {"label": "   ", "mentions": 4},  # blank once stripped
        {"label": 123, "mentions": 3},  # non-string
        "not-a-dict",  # type: ignore[list-item]
        _theme("quality", mentions=2),
    ]
    questions = themes_to_questions(themes)  # type: ignore[arg-type]
    assert questions == ["What do reviewers say about quality?"]


# ---------------------------------------------------------------------------
# build_suggestions: 3–4 result, fallback behavior
# ---------------------------------------------------------------------------


def test_build_returns_three_to_four_from_many_themes() -> None:
    """Plenty of themes → a theme-only set capped at the maximum."""
    themes = [_theme(f"t{i}", mentions=i) for i in range(1, 7)]
    result = build_suggestions(themes)
    assert MIN_SUGGESTIONS <= len(result) <= MAX_SUGGESTIONS
    assert all(q.startswith("What do reviewers say about t") for q in result)
    assert len(set(result)) == len(result)


def test_build_tops_up_short_theme_set_with_fallbacks() -> None:
    """Fewer than the minimum theme questions → topped up with distinct fallbacks."""
    result = build_suggestions([_theme("support", mentions=9)])
    assert len(result) == MAX_SUGGESTIONS
    assert result[0] == "What do reviewers say about support?"
    # The remainder are general fallback questions, none duplicated.
    assert result[1:] == list(FALLBACK_QUESTIONS[: MAX_SUGGESTIONS - 1])
    assert len(set(result)) == len(result)


def test_build_falls_back_when_no_themes() -> None:
    """No themes at all → the general fallback questions (Requirement 1.4)."""
    result = build_suggestions([])
    assert result == list(FALLBACK_QUESTIONS[:MAX_SUGGESTIONS])
    assert MIN_SUGGESTIONS <= len(result) <= MAX_SUGGESTIONS


def test_build_falls_back_when_themes_none() -> None:
    """``None`` themes (no active version/metrics) → the general fallback."""
    result = build_suggestions(None)
    assert result == list(FALLBACK_QUESTIONS[:MAX_SUGGESTIONS])


def test_build_falls_back_when_no_usable_labels() -> None:
    """Themes present but none have a usable label → the general fallback."""
    result = build_suggestions([{"mentions": 5}, {"label": "  "}])
    assert result == list(FALLBACK_QUESTIONS[:MAX_SUGGESTIONS])


def test_fallback_questions_are_distinct_and_meet_minimum() -> None:
    """The fallback list itself holds at least the minimum distinct questions."""
    assert len(FALLBACK_QUESTIONS) >= MIN_SUGGESTIONS
    assert len(set(FALLBACK_QUESTIONS)) == len(FALLBACK_QUESTIONS)


# ---------------------------------------------------------------------------
# load_suggestions: thin reader over the faked metrics read
# ---------------------------------------------------------------------------


def test_load_suggestions_builds_from_active_themes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reader delegates the faked themes to the pure builder."""
    monkeypatch.setattr(
        suggestions,
        "_active_themes",
        lambda dataset_id: [_theme("customer support", mentions=10), _theme("pricing", mentions=8)],
    )
    result = suggestions.load_suggestions(_DS)
    assert "What do reviewers say about customer support?" in result
    assert "What do reviewers say about pricing?" in result


def test_load_suggestions_falls_back_without_themes(monkeypatch: pytest.MonkeyPatch) -> None:
    """No themes from the read (unknown id / no active version) → fallback."""
    monkeypatch.setattr(suggestions, "_active_themes", lambda dataset_id: [])
    result = suggestions.load_suggestions(_DS)
    assert result == list(FALLBACK_QUESTIONS[:MAX_SUGGESTIONS])
