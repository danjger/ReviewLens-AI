"""Offline tests for the scope-guard evaluation suite (guardrailed-chat Task 7.1).

These exercise the suite's data and loader without a live model: ``cases.yaml``
parses into typed cases, the seed holds at least 60 cases with the required
category distribution, every case carries the required fields with a coherent
label/category/expect combination, and the fixture Corpus loads through the real
chat loader with the planted injection review present and the expected number of
reviews. The grader (Task 7.2), the CI job (Task 7.3), and the live run (Task
7.4) are out of scope here, so nothing in this test calls the AI.
"""

from __future__ import annotations

from evals.scope_guard.cases import (
    CATEGORIES,
    EXPECTATIONS,
    LABELS,
    MIN_CASES,
    PLANTED_INJECTION_REVIEW_ID,
    REQUIRED_COUNTS,
    counts_by_group,
    load_cases,
    load_fixture_dataset,
    missing_required_counts,
)

# The fixture reviews/v1.json carries exactly this many reviews; the loader must
# surface all of them (none are dropped by the token budget at this size).
EXPECTED_REVIEW_COUNT = 30


def test_cases_yaml_parses_and_every_case_has_required_fields() -> None:
    """Every case parses with a non-empty id/question and valid enum fields."""
    cases = load_cases()
    assert cases, "expected a non-empty case set"

    seen_ids: set[str] = set()
    for case in cases:
        assert case.id and case.id not in seen_ids, f"duplicate or empty id: {case.id!r}"
        seen_ids.add(case.id)
        assert case.question.strip(), f"{case.id}: question must be non-empty"
        assert case.label in LABELS, f"{case.id}: bad label {case.label!r}"
        assert case.expect in EXPECTATIONS, f"{case.id}: bad expect {case.expect!r}"
        if case.label in {"in_scope", "borderline"}:
            assert case.category is None, f"{case.id}: {case.label} must have null category"
            assert case.expect == "answer", f"{case.id}: {case.label} must expect answer"
        else:
            assert case.category in CATEGORIES, f"{case.id}: bad category {case.category!r}"


def test_at_least_60_cases_with_required_category_distribution() -> None:
    """The seed meets the >=60-case minimum and every required group count."""
    cases = load_cases()
    assert len(cases) >= MIN_CASES, f"expected >= {MIN_CASES} cases, got {len(cases)}"

    missing = missing_required_counts(cases)
    assert not missing, f"seed is short on required groups: {missing}"

    # Sanity: the required counts themselves add up to the design's 60.
    assert sum(REQUIRED_COUNTS.values()) == 60


def test_every_group_is_represented() -> None:
    """Each required (label, category) group has at least its minimum count."""
    counts = counts_by_group(load_cases())
    for key, need in REQUIRED_COUNTS.items():
        assert counts.get(key, 0) >= need, f"group {key} has {counts.get(key, 0)} < {need}"


def test_injection_cases_decline_and_are_present() -> None:
    """Injection cases are all expected to decline, and there are at least six."""
    injections = [c for c in load_cases() if c.is_injection]
    assert len(injections) >= 6, "expected at least 6 injection cases (Requirement 8.1)"
    for case in injections:
        assert case.should_decline, f"{case.id}: injection cases must expect decline"


def test_fixture_corpus_loads_with_planted_injection_review() -> None:
    """The fixture Corpus loads with the right review count and the planted probe."""
    fixture = load_fixture_dataset()

    assert fixture.platform == "g2"
    assert fixture.original_url.startswith("http")
    assert fixture.entity_name == "Acme CRM"

    corpus = fixture.corpus
    assert corpus.version == 1
    assert len(corpus.reviews) == EXPECTED_REVIEW_COUNT
    assert corpus.total_review_count == EXPECTED_REVIEW_COUNT
    assert not corpus.is_truncated, "fixture should fit the token budget intact"

    assert fixture.has_planted_injection, "planted injection review must be present"
    planted = next(r for r in corpus.reviews if r.id == PLANTED_INJECTION_REVIEW_ID)
    assert "ignore" in planted.text.lower()
    assert "weather" in planted.text.lower()


def test_competitor_cases_reference_named_competitors() -> None:
    """Competitor-fact cases name a competitor the reviews mention (Salesforce/HubSpot)."""
    cases = [c for c in load_cases() if c.category == "competitor_facts"]
    assert cases, "expected competitor-fact cases"
    for case in cases:
        lowered = case.question.lower()
        assert "salesforce" in lowered or "hubspot" in lowered, (
            f"{case.id}: competitor case should name Salesforce or HubSpot"
        )
