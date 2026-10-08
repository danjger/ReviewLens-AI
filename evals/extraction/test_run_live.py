"""Live extraction evaluation (Task 9.4) — scores the AI-backed methods.

This test is marked ``live_ai`` so the default backend test run skips it; it runs
only when live AI is explicitly selected and an ``ANTHROPIC_API_KEY`` is
configured (``make eval`` / Task 9.4). It scores every method including the
automatic choice, writes ``report.md``, and asserts the Requirement 8.3
thresholds on the automatic choice over ``will_work`` pages.

Invoke per structure.md (repo-root ``/evals``)::

    cd backend && uv run pytest ../evals -v -m live_ai

The offline structure/seed/scorer checks live in the backend unit tests
(``tests/unit/evals/test_extraction_eval.py``) and run with no API key.
"""

from __future__ import annotations

import os

import pytest

from evals.extraction import scorer
from evals.extraction.run import run_evaluation


@pytest.mark.live_ai
def test_live_evaluation() -> None:
    """Score all methods live, write the report, and assert the thresholds."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set; live evaluation requires the real model")

    report = run_evaluation(include_ai=True)
    passed, precision, recall = scorer.auto_meets_thresholds(report)

    assert precision is not None and recall is not None, "automatic choice was not scored"
    assert passed, (
        f"Automatic choice below thresholds: precision={precision:.3f} "
        f"(need ≥ {scorer.AUTO_PRECISION_THRESHOLD}), recall={recall:.3f} "
        f"(need ≥ {scorer.AUTO_RECALL_THRESHOLD})."
    )

    # Viability verdict accuracy (dataset-ingestion Requirement 3.14): the
    # verdict from assess() is scored against every labelled page and must meet
    # the 0.90 threshold, or the evaluation fails.
    verdict_passed, verdict_accuracy = scorer.verdict_meets_threshold(report)
    assert verdict_accuracy is not None, "viability verdicts were not scored"
    assert verdict_passed, (
        f"Viability verdict accuracy below threshold: {verdict_accuracy:.3f} "
        f"(need ≥ {scorer.VERDICT_ACCURACY_THRESHOLD})."
    )
