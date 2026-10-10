"""Gated live-AI RECORDING test for the Review Locator over the eval seed pages.

What this is
------------
This is a *gated* live-AI recording test, a sibling of
``test_locator_record_large_fixture.py``. Under ``make record-ai``
(``RECORD_AI=1`` with an ``ANTHROPIC_API_KEY``, run via
``pytest tests/ -m live_ai``) it drives the real Review Locator over every
labelled extraction-eval page by running the SAME scorer the live evaluation
runs (``evals.extraction.run.run_evaluation(include_ai=True)``). That scorer
touches each page through the exact AI-backed paths the live eval uses — the
``ai_direct`` method (``extract_page``), the automatic choice (``build_plan`` +
``extract_page``), and the viability verdict (``ingestion.viability.assess``) —
so every ``messages.create`` request is recorded under ``tests/fixtures/ai/``
with a key that matches what the live eval later replays offline.

Why it lives here (and not only in ``evals/``)
----------------------------------------------
``make record-ai`` runs ``pytest tests/ -m live_ai``; the eval suite under
``/evals`` is run separately by ``make eval``. Recording the seed's Locator
fixtures here means a single ``make record-ai`` captures both the large-fixture
Locator call and every seed page's Locator call, so the committed fixtures let
the offline suite and the live extraction eval (``review-extraction`` task 9.4,
including the dataset-ingestion Requirement 3.14 verdict-accuracy gate) replay
with no network.

References: ``review-extraction`` tasks 9.3 / 9.4; dataset-ingestion
Requirement 3.14 (viability verdict accuracy >= 0.90 over the seed).

Running / replaying
-------------------
Running this with ``make record-ai`` writes the fixtures; CI then replays them
offline (no key, no network). The test skips cleanly when no
``ANTHROPIC_API_KEY`` is set so a normal ``make test`` / ``-m live_ai`` run
without a key never fails. When a key *is* present it runs: in record mode it
captures each missing fixture, and if the fixtures already exist it simply
replays them and still asserts the Requirement 8.3 extraction thresholds and
the Requirement 3.14 verdict-accuracy gate.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from app.core.ai import AiClient, reset_ai_client, set_ai_client
from app.core.config import get_settings
from evals.extraction import scorer
from evals.extraction.run import run_evaluation
from moto import mock_aws

from tests.support.ai import FakeClaude
from tests.support.dynamodb import ensure_rate_limit_table

_TABLE = "rate-limits"
_REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Pin AWS env + reset the settings/client caches around the recording run.

    Mirrors the sibling large-fixture recording test: the instrumented client
    enforces a global AI rate limit backed by the ``rate-limits`` DynamoDB
    table, so the body runs under ``@mock_aws`` with the table created.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    reset_ai_client()


@pytest.mark.live_ai
@mock_aws
def test_record_eval_seed_locator_fixtures() -> None:
    """Drive the real Locator over the whole eval seed and assert the gates.

    Under ``make record-ai`` this records the Locator fixtures for every seed
    page; CI replays them offline. Skips when no ``ANTHROPIC_API_KEY`` is set so
    an unkeyed run never fails.

    Validates: Requirements 8.3, 3.14
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip(
            "no ANTHROPIC_API_KEY: this gated live-AI recording test records the "
            "Review Locator fixtures for every extraction-eval seed page "
            "(run `make record-ai`). CI replays the committed fixtures offline."
        )

    # The rate-limit table must exist before the instrumented client runs.
    ensure_rate_limit_table(_TABLE, region=_REGION)

    # Record mode engages only under RECORD_AI (+ key); otherwise this replays
    # the existing fixtures. Reset the process-wide client in teardown.
    set_ai_client(AiClient(client=FakeClaude.from_env()))
    try:
        # Same entry point the live evaluation (`make eval`) uses, so the
        # recorded fixture keys match what the eval replays. This scores every
        # method (structured, selectors, ai_direct, auto) and the viability
        # verdict for every labelled page.
        report = run_evaluation(include_ai=True)

        # Requirement 8.3: the automatic choice meets its precision/recall gate.
        auto_passed, precision, recall = scorer.auto_meets_thresholds(report)
        assert precision is not None and recall is not None, "automatic choice was not scored"
        assert auto_passed, (
            f"Automatic choice below thresholds: precision={precision:.3f} "
            f"(need >= {scorer.AUTO_PRECISION_THRESHOLD}), recall={recall:.3f} "
            f"(need >= {scorer.AUTO_RECALL_THRESHOLD})."
        )

        # Requirement 3.14: the viability verdict accuracy meets its gate now
        # that the six will_work seed pages carry >= 5 reviews.
        verdict_passed, verdict_accuracy = scorer.verdict_meets_threshold(report)
        assert verdict_accuracy is not None, "viability verdicts were not scored"
        assert verdict_passed, (
            f"Viability verdict accuracy below threshold: {verdict_accuracy:.3f} "
            f"(need >= {scorer.VERDICT_ACCURACY_THRESHOLD})."
        )
    finally:
        reset_ai_client()
