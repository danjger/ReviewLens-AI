"""Shared pytest configuration for the backend test suite.

The one behaviour defined here is a safety net: by default every test runs with
the process-wide AI client pointed at the offline :class:`FakeClaude` stub, so
no test can accidentally reach the network.  Tests that need specific recorded
responses still inject their own ``AiClient(client=FakeClaude(...))`` with
``set_ai_client`` inside the test; this autouse fixture only sets the default
and restores it afterwards (see ``.kiro/steering/testing.md``: "tests use
``FakeClaude``").

``FakeClaude`` replays recorded ``messages.create`` fixtures (raising
``MissingFixtureError`` when one is missing, so a test that genuinely needs a
fixture still fails loudly) and estimates ``messages.count_tokens`` offline, so
the Extraction Engine's token-budget logic is exercised without a live model.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.core.ai import AiClient, reset_ai_client, set_ai_client

from tests.support.ai import FakeClaude


@pytest.fixture(autouse=True)
def _offline_ai_client() -> Iterator[None]:
    """Point the process-wide AI client at the offline stub for each test."""
    set_ai_client(AiClient(client=FakeClaude()))
    try:
        yield
    finally:
        reset_ai_client()


@pytest.fixture(autouse=True)
def _isolate_aws_from_localstack(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep moto-backed unit/property tests off the shared LocalStack endpoint.

    The local environment sets ``AWS_ENDPOINT_URL=http://localhost:4566`` so the
    app talks to the Compose LocalStack. moto 5 honours that variable, so any
    ``@mock_aws`` unit test would silently reach the *real* LocalStack instead of
    moto's in-memory backend. LocalStack's tables already exist and persist, so
    ``create_table`` collides with ``ResourceInUseException: Table already
    exists`` and fails every test after the first — even when a file is run on
    its own. Scrubbing the endpoint (and pinning dummy credentials/region) makes
    moto use its in-memory backend, which starts empty and resets per test.

    Scoped to ``tests/unit`` and ``tests/property`` only; integration, scale, and
    perf tests deliberately point at LocalStack and set the endpoint in their own
    fixtures.
    """
    node_path = str(request.node.fspath)
    if "/tests/unit/" not in node_path and "/tests/property/" not in node_path:
        return
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
