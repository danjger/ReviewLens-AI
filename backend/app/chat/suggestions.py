"""Starter-question suggestions (guardrailed-chat Task 5.3).

The chat panel shows 3–4 suggested starter questions so an analyst has
something to click before typing their own (Requirement 1.4). The questions are
built from the dataset's **themes** using fixed templates — there is **no AI
call** here (design "Endpoints": "3–4 starter questions built from theme labels
using templates … no AI call"). When no themes are available (the dataset has
no active version yet, or its ``metrics`` carries no themes), a fixed list of
general questions that fit any review set is returned instead.

Where the themes come from
--------------------------
review-analysis (``app.handlers.metrics``) writes the active version's headline
numbers into the dataset's ``metrics`` dict, whose ``themes`` entries are shaped
``{label, mentions, lean}`` (design "Data Models → ``metrics``"; Requirement
5.3). That dict lives on the ``Dataset`` row (``Dataset.metrics``, a JSONB
column written "for the active version only"), which is exactly what the
Ingestion Summary / Library reads surface (``app.datasets.library`` reads
``dataset.metrics``; ``app.datasets.summary`` reads the active version's data).
This module **reuses that same read** — it loads ``Dataset.metrics`` through
:mod:`app.core.db` and takes ``metrics["themes"]`` — rather than re-deriving
themes or recomputing metrics. If ``metrics`` is absent (no active version), the
read yields no themes and the general fallback is used.

Everything except the one database read is a pure function
(:func:`build_suggestions`, :func:`themes_to_questions`), so the template and
cap/ordering/dedup logic is unit-testable without a database. Integration
against LocalStack is Task 5.4.

Like the history and save endpoints, the suggestions endpoint is an **ordinary
API-service route** (design "Endpoints"); its router (:mod:`app.chat.suggestions_api`)
is mounted under ``/api`` by :mod:`app.api`, making the path
``GET /api/datasets/{id}/chat/suggestions``. This module deliberately does
**not** import :mod:`app.chat.service` (the streaming app and AI runtime), so no
import cycle or heavy dependency is pulled into the API process — it uses only
:mod:`app.core.db` and the ``Dataset`` model.
"""

from __future__ import annotations

from typing import Any

from app.core.db import session_scope
from app.db.models import Dataset

# ---------------------------------------------------------------------------
# Tunables / templates (design "Endpoints"; Requirement 1.4)
# ---------------------------------------------------------------------------

#: How many starter questions to return. Requirement 1.4 asks for "3 to 4"; we
#: return at most :data:`MAX_SUGGESTIONS` and, from themes, aim for a full set by
#: topping up with fallbacks when there are too few distinct themes.
MAX_SUGGESTIONS = 4

#: Lower bound from Requirement 1.4 ("3 to 4"). When themes yield at least this
#: many questions we return a theme-only set; otherwise we top the set up with
#: general fallback questions so the panel always shows a useful minimum.
MIN_SUGGESTIONS = 3

#: Template for a theme-derived starter question. The example in Requirement 1.4
#: — "What do reviewers say about customer support?" from a "customer support"
#: theme — is exactly this template applied to the theme label.
_THEME_TEMPLATE = "What do reviewers say about {label}?"

#: General starter questions that fit any review set, used when no themes are
#: available (design "Endpoints"; Requirement 1.4). Ordered by usefulness; the
#: first :data:`MAX_SUGGESTIONS` are returned, and the leading ones also top up
#: a short theme-derived set. Kept distinct from each other so dedup never
#: shrinks the fallback below the minimum.
FALLBACK_QUESTIONS: tuple[str, ...] = (
    "What are the most common complaints?",
    "What do reviewers like most?",
    "How do ratings break down?",
    "Has sentiment changed over time?",
)


# ---------------------------------------------------------------------------
# Pure template logic
# ---------------------------------------------------------------------------


def _theme_label(theme: object) -> str | None:
    """Return a usable label string from one ``metrics.themes`` entry, or ``None``.

    A theme is the design's ``{label, mentions, lean}`` dict. Only the ``label``
    is needed to build a question. A non-dict entry, a missing/non-string label,
    or a label that is blank once stripped yields ``None`` so it is skipped —
    the metrics dict is trusted input, but a defensive skip keeps one odd row
    from producing an empty "What do reviewers say about ?" question.
    """
    if not isinstance(theme, dict):
        return None
    label = theme.get("label")
    if not isinstance(label, str):
        return None
    stripped = label.strip()
    return stripped or None


def _theme_mentions(theme: object) -> int:
    """Return a theme's ``mentions`` count for ordering, or ``0`` when absent.

    Used only as the sort key when selecting the top themes under the cap
    (design: keep the most-mentioned themes). A missing or non-integer
    ``mentions`` sorts as ``0`` so a malformed row never crashes the ordering.
    """
    mentions = theme.get("mentions") if isinstance(theme, dict) else None
    return mentions if isinstance(mentions, int) else 0


def themes_to_questions(themes: list[dict[str, Any]], *, limit: int = MAX_SUGGESTIONS) -> list[str]:
    """Build starter questions from theme labels, newest-first by mentions.

    Pure function (design "Endpoints": templates, no AI call). Steps:

    1. Order the themes by ``mentions`` descending so the most-discussed topics
       win when there are more than *limit* themes; ties keep the input order
       (``sorted`` is stable), matching the themes task's own tie rule.
    2. Apply the :data:`_THEME_TEMPLATE` to each theme's label, skipping entries
       with no usable label (:func:`_theme_label`).
    3. Deduplicate by the produced question (two themes whose labels differ only
       by case or surrounding space would otherwise produce the same question),
       preserving first-seen order.
    4. Cap at *limit* questions.

    Returns an empty list when *themes* is empty or none of the entries carry a
    usable label — the caller then falls back to the general questions.
    """
    ordered = sorted(themes, key=_theme_mentions, reverse=True)

    questions: list[str] = []
    seen: set[str] = set()
    for theme in ordered:
        label = _theme_label(theme)
        if label is None:
            continue
        question = _THEME_TEMPLATE.format(label=label)
        dedup_key = question.casefold()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        questions.append(question)
        if len(questions) >= limit:
            break
    return questions


def build_suggestions(themes: list[dict[str, Any]] | None) -> list[str]:
    """Return 3–4 starter questions for a dataset (Requirement 1.4).

    Pure function of the dataset's themes:

    - When *themes* yields at least :data:`MIN_SUGGESTIONS` distinct questions,
      return the top :data:`MAX_SUGGESTIONS` theme-derived questions.
    - When themes yield **some** but fewer than the minimum, top the set up with
      general :data:`FALLBACK_QUESTIONS` (skipping any that duplicate a
      theme-derived question) so the panel always shows at least the minimum.
    - When *themes* is ``None`` or empty, or none of the entries have a usable
      label, return the general fallback questions (design "Endpoints";
      Requirement 1.4).

    The result is always 3–4 questions (as long as the fallback list holds at
    least :data:`MIN_SUGGESTIONS` distinct entries, which it does) and never
    contains duplicates.
    """
    theme_questions = themes_to_questions(themes or [], limit=MAX_SUGGESTIONS)

    if len(theme_questions) >= MIN_SUGGESTIONS:
        return theme_questions[:MAX_SUGGESTIONS]

    # Too few (or no) theme questions: top up with distinct fallbacks.
    suggestions = list(theme_questions)
    seen = {question.casefold() for question in suggestions}
    for fallback in FALLBACK_QUESTIONS:
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
        if fallback.casefold() in seen:
            continue
        seen.add(fallback.casefold())
        suggestions.append(fallback)
    return suggestions


# ---------------------------------------------------------------------------
# DB read (the only impure part) — reuses the Summary/Library metrics path
# ---------------------------------------------------------------------------


def _active_themes(dataset_id: str) -> list[dict[str, Any]]:
    """Read the active version's themes from ``Dataset.metrics["themes"]``.

    Reuses the existing metrics read (``Dataset.metrics`` is the same JSONB the
    Library and Ingestion Summary surface, written by review-analysis for the
    active version only) through :mod:`app.core.db`. Returns an empty list when
    the dataset is unknown, has no ``metrics`` yet (no active version), or its
    metrics carry no ``themes`` list — in every one of those cases the caller
    falls back to the general questions. The no-sign-in app does not leak
    whether an id exists, so an unknown id is an empty-themes read, not an error.
    """
    with session_scope() as session:
        dataset = session.get(Dataset, dataset_id)
        metrics = dataset.metrics if dataset is not None else None

    if not isinstance(metrics, dict):
        return []
    themes = metrics.get("themes")
    if not isinstance(themes, list):
        return []
    return [theme for theme in themes if isinstance(theme, dict)]


def load_suggestions(dataset_id: str) -> list[str]:
    """Return the 3–4 starter questions for *dataset_id* (Requirement 1.4).

    Thin reader: pulls the active version's themes from ``Dataset.metrics``
    (reusing the Summary/Library metrics path) and delegates to the pure
    :func:`build_suggestions`. No AI call is made.
    """
    return build_suggestions(_active_themes(dataset_id))
