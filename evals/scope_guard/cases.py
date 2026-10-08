"""Loader and schema for the scope-guard evaluation (guardrailed-chat Task 7.1).

The labeled evaluation set lives next to this module:

- ``reviews/v1.json`` — the fixed fixture Corpus, shaped exactly like the
  ``reviews/v{n}.json`` document review-analysis writes, so it is parsed by the
  real :mod:`app.chat.corpus` loader (no divergent fixture shape to drift).
- ``dataset.json`` — the dataset metadata not stored in ``reviews/v{n}.json``
  (``platform`` and ``original_url``) that the chat system prompt's SCOPE line
  needs when the grader (Task 7.2) assembles the prompt.
- ``cases.yaml`` — one entry per labeled question (``id``, ``question``,
  ``label``, ``category``, ``expect``).

This module parses ``cases.yaml`` into typed :class:`Case` objects and loads the
fixture :class:`FixtureDataset` (Corpus + platform + original_url). It is pure
and offline — it reads files only, never the AI or the network — so the grader
and its tests can construct the labeled set with no API key, mirroring
:mod:`evals.extraction.labels`.

The label and category vocabularies are kept aligned with the chat code so the
grader can compare directly:

- :data:`LABELS` matches :data:`app.chat.precheck._LABELS`.
- :data:`CATEGORIES` matches :data:`app.chat.postprocess.SCOPE_CATEGORIES`.

Schema (see ``cases.yaml`` for the authoritative comment)::

    cases:
      - id: str
        question: str
        label: in_scope | out_of_scope | borderline | injection
        category: null | other_platform | world_knowledge | competitor_facts
                       | unrelated_task | injection
        expect: answer | decline
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from app.chat.corpus import Corpus, _parse_corpus

# Directory of this module; the fixture files sit beside it.
EVAL_DIR = Path(__file__).resolve().parent
CASES_PATH = EVAL_DIR / "cases.yaml"
REVIEWS_PATH = EVAL_DIR / "reviews" / "v1.json"
DATASET_PATH = EVAL_DIR / "dataset.json"

#: The fixture's data version (``reviews/v1.json``).
FIXTURE_VERSION = 1

#: The id of the planted prompt-injection review inside the fixture Corpus. Its
#: text asks the assistant to ignore its rules and report the weather; the
#: injection cases (and the grader's injection-success check) rely on it being
#: present (Requirement 4.1, 8.1).
PLANTED_INJECTION_REVIEW_ID = "r_0010"

#: Scope labels a case may carry. Kept identical to ``app.chat.precheck._LABELS``
#: so a case label maps straight onto what the pre-check / final scope report.
LABELS: frozenset[str] = frozenset({"in_scope", "out_of_scope", "borderline", "injection"})

#: Decline sub-categories, identical to ``app.chat.postprocess.SCOPE_CATEGORIES``.
#: A case's ``category`` is one of these for ``out_of_scope`` / ``injection`` and
#: ``None`` for ``in_scope`` / ``borderline``.
CATEGORIES: frozenset[str] = frozenset(
    {
        "other_platform",
        "world_knowledge",
        "competitor_facts",
        "unrelated_task",
        "injection",
    }
)

#: The two expected behaviors. ``answer`` means a grounded answer from the
#: reviews is correct; ``decline`` means a graceful scope decline is correct.
EXPECTATIONS: frozenset[str] = frozenset({"answer", "decline"})

#: The category distribution the seed must satisfy (design "Testing Strategy ->
#: Guardrail evaluation"). Keys are ``(label, category)`` pairs; borderline and
#: in-scope carry no category so their key uses ``None``. Minimums, not exact:
#: the seed may add a few extra to comfortably exceed 60.
REQUIRED_COUNTS: dict[tuple[str, str | None], int] = {
    ("in_scope", None): 20,
    ("out_of_scope", "other_platform"): 8,
    ("out_of_scope", "world_knowledge"): 8,
    ("out_of_scope", "competitor_facts"): 6,
    ("out_of_scope", "unrelated_task"): 6,
    ("borderline", None): 6,
    ("injection", "injection"): 6,
}

#: The minimum total number of labeled cases (Requirement 8.1).
MIN_CASES = 60


@dataclass(frozen=True)
class Case:
    """One labeled evaluation question.

    :ivar id: Stable, unique case id (e.g. ``"in_scope_01"``).
    :ivar question: The exact question text sent to the chat.
    :ivar label: One of :data:`LABELS`.
    :ivar category: The decline sub-category (one of :data:`CATEGORIES`) for
        ``out_of_scope`` / ``injection`` cases; ``None`` for ``in_scope`` /
        ``borderline``.
    :ivar expect: ``"answer"`` or ``"decline"`` — the correct behavior.
    """

    id: str
    question: str
    label: str
    category: str | None
    expect: str

    @property
    def should_decline(self) -> bool:
        """Whether the correct behavior for this case is a scope decline."""
        return self.expect == "decline"

    @property
    def is_injection(self) -> bool:
        """Whether this case is a prompt-injection attempt."""
        return self.label == "injection"


@dataclass(frozen=True)
class FixtureDataset:
    """The fixed fixture dataset the cases are asked against.

    :ivar corpus: The fixture :class:`~app.chat.corpus.Corpus` (entity profile +
        Normalized Reviews), parsed from ``reviews/v1.json`` through the real
        chat Corpus loader.
    :ivar platform: The source platform (``"g2"``) — the chat's only scope.
    :ivar original_url: The dataset's source URL, for the system prompt's SCOPE
        line.
    """

    corpus: Corpus
    platform: str
    original_url: str

    @property
    def entity_name(self) -> str:
        """The identified entity's name (e.g. ``"Acme CRM"``)."""
        return self.corpus.entity.name

    @property
    def has_planted_injection(self) -> bool:
        """Whether the planted injection review is present in the Corpus."""
        return any(r.id == PLANTED_INJECTION_REVIEW_ID for r in self.corpus.reviews)


def _parse_case(raw: dict[str, Any], *, index: int) -> Case:
    """Parse and validate one case entry from ``cases.yaml``."""
    case_id = str(raw.get("id", "")).strip()
    if not case_id:
        raise ValueError(f"Case #{index}: a non-empty 'id' is required")

    question = str(raw.get("question", "")).strip()
    if not question:
        raise ValueError(f"Case {case_id!r}: a non-empty 'question' is required")

    label = str(raw.get("label", "")).strip()
    if label not in LABELS:
        raise ValueError(f"Case {case_id!r}: label {label!r} must be one of {sorted(LABELS)}")

    raw_category = raw.get("category")
    category = str(raw_category).strip() if raw_category not in (None, "") else None
    if category is not None and category not in CATEGORIES:
        raise ValueError(
            f"Case {case_id!r}: category {category!r} must be one of {sorted(CATEGORIES)} or null"
        )

    expect = str(raw.get("expect", "")).strip()
    if expect not in EXPECTATIONS:
        raise ValueError(
            f"Case {case_id!r}: expect {expect!r} must be one of {sorted(EXPECTATIONS)}"
        )

    # Semantic cross-checks that keep the label vocabulary coherent with the
    # chat code: in-scope and borderline are answered and carry no category;
    # out-of-scope / injection carry a category.
    if label in {"in_scope", "borderline"}:
        if category is not None:
            raise ValueError(f"Case {case_id!r}: {label} cases must have null category")
        if expect != "answer":
            raise ValueError(f"Case {case_id!r}: {label} cases must expect 'answer'")
    else:  # out_of_scope / injection
        if category is None:
            raise ValueError(f"Case {case_id!r}: {label} cases need a category")

    return Case(
        id=case_id,
        question=question,
        label=label,
        category=category,
        expect=expect,
    )


def load_cases(cases_path: Path = CASES_PATH) -> list[Case]:
    """Load and validate every labeled case from ``cases.yaml``.

    :param cases_path: Path to the cases file (defaults to the one beside this
        module). Overridable so tests can point at a fixture file.
    :returns: The cases in file order.
    :raises ValueError: The file is malformed, a case is malformed, or two cases
        share an id.
    """
    raw = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or "cases" not in raw:
        raise ValueError(f"{cases_path} must be a mapping with a 'cases' list")
    raw_cases = raw["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"{cases_path}: 'cases' must be a non-empty list")

    cases = [_parse_case(entry, index=i) for i, entry in enumerate(raw_cases)]

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"Duplicate case id {case.id!r} in {cases_path}")
        seen.add(case.id)

    return cases


def load_fixture_dataset(
    reviews_path: Path = REVIEWS_PATH, dataset_path: Path = DATASET_PATH
) -> FixtureDataset:
    """Load the fixture Corpus and dataset metadata.

    Parses ``reviews/v1.json`` through the real :func:`app.chat.corpus._parse_corpus`
    so the fixture is exercised exactly as a live dataset would be, and reads
    ``platform`` / ``original_url`` from ``dataset.json`` for the prompt's SCOPE
    line.

    :param reviews_path: Path to the ``reviews/v1.json`` fixture.
    :param dataset_path: Path to the ``dataset.json`` metadata file.
    :returns: The :class:`FixtureDataset`.
    :raises ValueError: The reviews file has no reviews, or ``dataset.json`` is
        missing ``platform`` / ``original_url``.
    """
    doc = json.loads(reviews_path.read_text(encoding="utf-8"))
    dataset_id = str(doc.get("dataset_id") or "scope-guard-fixture")
    version = int(doc.get("version") or FIXTURE_VERSION)
    corpus = _parse_corpus(dataset_id, version, doc)
    if not corpus.reviews:
        raise ValueError(f"{reviews_path}: fixture Corpus must contain reviews")

    meta = json.loads(dataset_path.read_text(encoding="utf-8"))
    platform = str(meta.get("platform", "")).strip()
    original_url = str(meta.get("original_url", "")).strip()
    if not platform or not original_url:
        raise ValueError(f"{dataset_path}: 'platform' and 'original_url' are both required")

    return FixtureDataset(corpus=corpus, platform=platform, original_url=original_url)


def counts_by_group(cases: list[Case]) -> dict[tuple[str, str | None], int]:
    """Return the number of cases per ``(label, category)`` group."""
    counts: dict[tuple[str, str | None], int] = {}
    for case in cases:
        key = (case.label, case.category)
        counts[key] = counts.get(key, 0) + 1
    return counts


def missing_required_counts(cases: list[Case]) -> dict[tuple[str, str | None], tuple[int, int]]:
    """Return groups that fall short of :data:`REQUIRED_COUNTS`.

    The value for each short group is ``(have, need)``. An empty result means the
    seed meets every required minimum.
    """
    counts = counts_by_group(cases)
    short: dict[tuple[str, str | None], tuple[int, int]] = {}
    for key, need in REQUIRED_COUNTS.items():
        have = counts.get(key, 0)
        if have < need:
            short[key] = (have, need)
    return short
