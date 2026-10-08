"""Live scope-guard evaluation (Task 7.4) — grades every case with the model.

This test is marked ``live_ai`` so the default backend test run skips it; it runs
only when live AI is explicitly selected and an ``ANTHROPIC_API_KEY`` is
configured (``make eval`` / Task 7.4). It grades every labeled case through the
real chat flow (assemble → main model → post-process → LLM-judge), writes
``report.md``, and asserts the four Requirement 8.3 thresholds.

Invoke per structure.md (repo-root ``/evals``)::

    cd backend && uv run pytest ../evals -v -m live_ai

The offline grading / metric / report checks live in the backend unit tests
(``tests/unit/evals/test_scope_guard_grader.py``) and run with no API key.
"""

from __future__ import annotations

import os

import pytest

from evals.scope_guard import grader
from evals.scope_guard.run import run_evaluation


@pytest.mark.live_ai
def test_live_evaluation() -> None:
    """Grade all cases live, write the report, and assert the thresholds."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set; live evaluation requires the real model")

    report = run_evaluation(include_ai=True)
    m = report.metrics
    thresholds = grader.meets_thresholds(m)

    assert thresholds.passed, (
        "Scope-guard thresholds not met: "
        f"correct_decline={m.correct_decline_rate} (need ≥ {grader.CORRECT_DECLINE_THRESHOLD}), "
        f"false_decline={m.false_decline_rate} (need ≤ {grader.FALSE_DECLINE_THRESHOLD}), "
        f"injection_successes={m.injection_successes} (need ≤ {grader.MAX_INJECTION_SUCCESSES}), "
        f"citation_validity={m.citation_validity} (need = {grader.CITATION_VALIDITY_THRESHOLD})."
    )
