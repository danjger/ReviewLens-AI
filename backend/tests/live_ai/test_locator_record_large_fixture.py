"""Gated live-AI RECORDING test for the Review Locator over the large fixture.

What this is
------------
This is a *gated* live-AI recording test. Under ``make record-ai``
(``RECORD_AI=1`` with an ``ANTHROPIC_API_KEY``, run via
``pytest tests/ -m live_ai``) it drives the real Review Locator over the large
server-rendered fixture page through :func:`app.ingestion.viability.assess` —
the SAME code path the extraction eval scorer and the HTML-upload path use — so
the recorded ``messages.create`` fixture(s) land under ``tests/fixtures/ai/``
with a key that matches those paths. The committed fixture then lets the offline
suite, the extraction eval verdict-accuracy column (``review-extraction`` task
9.4), and the ``dataset-ingestion`` HTML-upload E2E ``will_work`` case replay a
genuine ``will_work`` with no network.

References: ``review-extraction`` task 9.4; ``dataset-ingestion`` Requirement
9.2 (the large fixture reaches a genuine ``will_work`` verdict) and Requirement
9.3 (one source page serves both the URL-check and HTML-upload paths).

Running / replaying
-------------------
Running this with ``make record-ai`` writes the fixture; CI then replays it
offline (no key, no network). The test skips cleanly when no
``ANTHROPIC_API_KEY`` is set so a normal ``make test`` / ``-m live_ai`` run
without a key never fails. When a key *is* present it runs: in record mode it
captures the fixture, and if a fixture already exists it simply replays it and
still asserts a genuine ``will_work``.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from app.core.ai import AiClient, reset_ai_client, set_ai_client
from app.core.config import get_settings
from app.ingestion.robots import RobotsResult
from app.ingestion.viability import CaptureView, assess
from evals.extraction.labels import load_labeled_pages
from moto import mock_aws

from tests.support.ai import FakeClaude
from tests.support.dynamodb import ensure_rate_limit_table

_TABLE = "rate-limits"
_REGION = "us-east-1"
_FIXTURE_NAME = "large_server_rendered"


# ---------------------------------------------------------------------------
# AWS env + rate-limit table: the instrumented client enforces a global AI rate
# limit backed by the ``rate-limits`` DynamoDB table, so the test body runs
# under ``@mock_aws`` with the table created. Mirrors the autouse fixture in
# ``tests/unit/extraction/test_locator_recorded.py``.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
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
def test_record_large_fixture_reaches_genuine_will_work() -> None:
    """Drive the real Locator over the large fixture and assert a genuine will_work.

    Under ``make record-ai`` this records the Locator fixture; CI replays it
    offline. Skips when no ``ANTHROPIC_API_KEY`` is set so an unkeyed run never
    fails.

    Validates: Requirements 9.2
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip(
            "no ANTHROPIC_API_KEY: this gated live-AI recording test records "
            "the Review Locator fixture for the large server-rendered page "
            "(run `make record-ai`). CI replays the committed fixture offline."
        )

    # The rate-limit table must exist before the instrumented client runs.
    ensure_rate_limit_table(_TABLE, region=_REGION)

    # Record mode engages only under RECORD_AI (+ key); otherwise this replays
    # an existing fixture. Reset the process-wide client in teardown.
    set_ai_client(AiClient(client=FakeClaude.from_env()))
    try:
        pages = {p.name: p for p in load_labeled_pages()}
        assert _FIXTURE_NAME in pages, (
            f"the large fixture {_FIXTURE_NAME!r} must be registered in labels.yaml"
        )
        page = pages[_FIXTURE_NAME]

        # Same path the eval scorer and the HTML-upload flow use, so the recorded
        # fixture key matches: clean → locate → postprocess → build_plan.
        verdict, _plan = assess(
            CaptureView(html=page.html, page_title="", main_status=200),
            final_url=page.url,
            robots=RobotsResult(allowed=True),
        )

        # The recorded/replayed result is a genuine will_work (Requirement 9.2):
        # enough verified reviews, no blocker, and confidence that is not low.
        assert verdict.verdict == "will_work"
        assert verdict.evidence.reviews_verified >= 5
        assert verdict.evidence.blocker is None
        assert verdict.evidence.locator_confidence != "low"
    finally:
        reset_ai_client()
