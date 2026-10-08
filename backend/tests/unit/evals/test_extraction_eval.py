"""Offline tests for the extraction evaluation suite (review-extraction Task 9.1).

These exercise the suite without a live model: the labels schema parses, the seed
covers every required layout type, and the scorer's offline paths (``structured``
and ``selectors``) produce a report / score object with no API key. The live
AI-backed scoring (``ai_direct`` / ``auto``, Task 9.4) is kept out of the default
run — here those methods are always reported not-applicable because
``include_ai=False``.
"""

from __future__ import annotations

from pathlib import Path

from evals.extraction import scorer
from evals.extraction.labels import (
    REQUIRED_LAYOUT_TAGS,
    load_labeled_pages,
    missing_required_tags,
)
from evals.extraction.report import render_report
from evals.extraction.run import run_evaluation


def test_labels_yaml_parses_and_loads_html() -> None:
    """Every labelled page parses and its saved HTML loads."""
    pages = load_labeled_pages()
    assert pages, "expected a non-empty labelled set"
    for page in pages:
        assert page.url.startswith("http"), f"{page.name}: url should be absolute"
        assert page.verdict in {"will_work", "limited", "wont_work"}
        assert page.html.strip(), f"{page.name}: HTML should be non-empty"


def test_seed_covers_every_required_layout_type() -> None:
    """The seed covers every layout type required by Requirement 8.1."""
    pages = load_labeled_pages()
    missing = missing_required_tags(pages)
    assert not missing, f"seed is missing required layout tags: {sorted(missing)}"
    # Sanity: the required set itself names the eight layout families.
    assert len(REQUIRED_LAYOUT_TAGS) == 8


def test_blocker_pages_have_no_expected_reviews() -> None:
    """Blocker pages are labelled wont_work with no expected reviews."""
    pages = {p.name: p for p in load_labeled_pages()}
    blockers = [p for p in pages.values() if "blocker" in p.tags]
    assert blockers, "expected at least one blocker page in the seed"
    for page in blockers:
        assert page.verdict == "wont_work"
        assert page.reviews == ()


def test_offline_scoring_produces_a_report_without_a_live_key() -> None:
    """The offline paths score and the report writes, with no AI call."""
    pages = load_labeled_pages()
    report = scorer.score_pages(pages, include_ai=False)

    assert len(report.pages) == len(pages)
    for page_score in report.pages:
        structured = page_score.methods[scorer.METHOD_STRUCTURED]
        selectors = page_score.methods[scorer.METHOD_SELECTORS]
        # Offline methods always ran; AI-backed ones are not applicable offline.
        assert page_score.methods[scorer.METHOD_AI_DIRECT].applicable is False
        assert page_score.methods[scorer.METHOD_AUTO].applicable is False
        # Offline methods never spend tokens.
        assert structured.tokens == 0
        assert selectors.tokens == 0

    markdown = render_report(report, missing_tags=missing_required_tags(pages))
    assert "# Extraction evaluation report" in markdown
    assert "offline (no live AI)" in markdown


def test_structured_method_finds_jsonld_reviews() -> None:
    """The structured method recovers the JSON-LD reviews with high recall."""
    pages = {p.name: p for p in load_labeled_pages()}
    page = pages["structured_jsonld"]
    report = scorer.score_pages([page], include_ai=False)
    structured = report.pages[0].methods[scorer.METHOD_STRUCTURED]
    # All three JSON-LD reviews are present in the visible text and recoverable.
    assert structured.recall == 1.0
    assert structured.precision == 1.0


def test_selectors_method_scores_plain_list() -> None:
    """The selectors method extracts the plain-list reviews via labelled selectors."""
    pages = {p.name: p for p in load_labeled_pages()}
    page = pages["plain_list"]
    report = scorer.score_pages([page], include_ai=False)
    selectors = report.pages[0].methods[scorer.METHOD_SELECTORS]
    assert selectors.applicable is True
    assert selectors.recall == 1.0
    assert selectors.precision == 1.0
    # Ratings are cue-backed (aria-label) and in range, so they score.
    assert selectors.rating.value == 1.0


def test_selectors_method_excludes_qa_and_seller_responses() -> None:
    """On the mixed page, the review-only selector does not pick up Q&A/seller text."""
    pages = {p.name: p for p in load_labeled_pages()}
    page = pages["mixed_qa_seller"]
    report = scorer.score_pages([page], include_ai=False)
    selectors = report.pages[0].methods[scorer.METHOD_SELECTORS]
    # Exactly the three customer reviews, no over-selection.
    assert selectors.extracted == 3
    assert selectors.precision == 1.0
    assert selectors.recall == 1.0


def test_selectors_detects_multipage_next_page() -> None:
    """The selectors run resolves the labelled next-page URL on the multi-page listing."""
    pages = {p.name: p for p in load_labeled_pages()}
    page = pages["multipage_listing"]
    report = scorer.score_pages([page], include_ai=False)
    selectors = report.pages[0].methods[scorer.METHOD_SELECTORS]
    assert selectors.next_page_correct is True


def test_auto_thresholds_not_evaluated_offline() -> None:
    """The automatic-choice thresholds cannot be met without a live run."""
    pages = load_labeled_pages()
    report = scorer.score_pages(pages, include_ai=False)
    passed, precision, recall = scorer.auto_meets_thresholds(report)
    assert passed is False
    assert precision is None and recall is None


def test_run_evaluation_writes_report(tmp_path: Path) -> None:
    """run_evaluation writes a Markdown report offline."""
    out = tmp_path / "report.md"
    report = run_evaluation(include_ai=False, out=out)
    assert out.exists()
    assert report.include_ai is False
    assert out.read_text(encoding="utf-8").startswith("# Extraction evaluation report")


# ---------------------------------------------------------------------------
# Viability verdict accuracy (dataset-ingestion Requirement 3.14)
# ---------------------------------------------------------------------------


def test_blocker_verdicts_scored_offline_with_no_ai() -> None:
    """Blocker pages are verdict-scored offline (pre-scan, no AI) as wont_work.

    Validates: Requirements 3.14
    """
    pages = {p.name: p for p in load_labeled_pages()}
    report = scorer.score_pages(list(pages.values()), include_ai=False)
    by_page = {v.page: v for v in report.verdicts}

    blockers = [p for p in pages.values() if "blocker" in p.tags]
    assert blockers, "expected at least one blocker page in the seed"
    for page in blockers:
        vscore = by_page[page.name]
        # Blocker pages short-circuit in the pre-scan, so they are scored even
        # offline, and assess() returns wont_work — matching the label.
        assert vscore.scored is True
        assert vscore.predicted == "wont_work"
        assert vscore.expected == "wont_work"
        assert vscore.correct is True


def test_non_blocker_verdicts_skipped_offline() -> None:
    """Pages needing an AI call are recorded not-scored on an offline run.

    Validates: Requirements 3.14
    """
    pages = load_labeled_pages()
    report = scorer.score_pages(pages, include_ai=False)
    by_page = {v.page: v for v in report.verdicts}

    non_blockers = [p for p in pages if "blocker" not in p.tags]
    assert non_blockers, "expected non-blocker pages in the seed"
    for page in non_blockers:
        vscore = by_page[page.name]
        assert vscore.scored is False
        assert vscore.predicted is None


def test_offline_verdict_subset_accuracy_is_perfect() -> None:
    """The offline-scorable (blocker) subset scores at full accuracy.

    Validates: Requirements 3.14
    """
    pages = load_labeled_pages()
    report = scorer.score_pages(pages, include_ai=False)
    agg = scorer.aggregate_verdict(report)
    # Only the blocker pages are scored offline; all are correctly wont_work.
    assert agg.total >= 1
    assert agg.correct == agg.total
    assert agg.accuracy == 1.0


def test_verdict_threshold_not_evaluated_offline() -> None:
    """The full-corpus verdict gate cannot be met without a live run.

    Mirrors ``auto_meets_thresholds``: offline the AI-backed verdicts are not
    scored, so accuracy is ``None`` and the gate does not pass.

    Validates: Requirements 3.14
    """
    pages = load_labeled_pages()
    report = scorer.score_pages(pages, include_ai=False)
    passed, accuracy = scorer.verdict_meets_threshold(report)
    assert passed is False
    assert accuracy is None


def test_verdict_section_rendered_in_report() -> None:
    """The report includes the verdict-accuracy section and threshold.

    Validates: Requirements 3.14
    """
    pages = load_labeled_pages()
    report = scorer.score_pages(pages, include_ai=False)
    markdown = render_report(report, missing_tags=missing_required_tags(pages))
    assert "Viability verdict accuracy" in markdown
    assert "Requirement 3.14" in markdown
