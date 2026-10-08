"""Unit tests for app.core.ai (the instrumented Anthropic client).

Covers:
- Logging: a successful call emits a structured ``ai_call`` log line carrying
  purpose, model, input/output tokens, cache-hit token fields, and latency_ms
  (Requirement 9.3).
- Global limit: when the global AI-call rate limit is exceeded the client
  raises AiRateLimitError and never invokes the underlying model
  (Requirements 2.4).
- The stub: FakeClaude replays a recorded fixture correctly and raises a clear
  MissingFixtureError when a fixture is missing (Requirement 8.3).

No test here calls the live AI; FakeClaude replays local fixtures and the model
delegate is a plain stub.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.core import ai as ai_module
from app.core.ai import (
    AiClient,
    AiRateLimitError,
    get_ai_client,
    reset_ai_client,
    resolve_model,
    set_ai_client,
)
from app.core.config import get_settings
from moto import mock_aws

from tests.support.ai import FakeClaude, MissingFixtureError, request_key
from tests.support.dynamodb import ensure_rate_limit_table

_TABLE = "rate-limits"
_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# Fixtures
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


def _create_rate_limit_table() -> None:
    """Create the ``rate-limits`` table (idempotent, shared schema)."""
    ensure_rate_limit_table(_TABLE, region=_REGION)


def _fake_response(
    *,
    input_tokens: int = 42,
    output_tokens: int = 12,
    cache_creation: int = 0,
    cache_read: int = 20,
) -> SimpleNamespace:
    """Build an SDK-shaped response object with a usage attribute."""
    return SimpleNamespace(
        id="msg_test",
        model="unused",
        content=[SimpleNamespace(type="text", text="hi")],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
    )


class _RecordingClient:
    """A minimal Anthropic-like client that records calls and returns a canned response."""

    def __init__(self, response: Any | None = None) -> None:  # noqa: ANN401
        self.calls: list[dict[str, Any]] = []
        self._response = response if response is not None else _fake_response()
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append(kwargs)
        return self._response


def _attach_json_logger() -> StringIO:
    """Install the JSON formatter on the root logger writing to a buffer."""
    from app.core.logging import _JsonFormatter, configure_logging  # noqa: PLC0415

    configure_logging(service_name="test-ai")
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    return stream


def _last_ai_log(stream: StringIO) -> dict[str, Any]:
    stream.seek(0)
    records = [json.loads(line) for line in stream.read().splitlines() if line.strip()]
    ai_records = [r for r in records if r.get("event") == "ai_call"]
    assert ai_records, "No ai_call log line emitted"
    return ai_records[-1]


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


class TestResolveModel:
    def test_precheck_uses_precheck_model(self) -> None:
        assert resolve_model("precheck") == get_settings().claude_precheck_model

    def test_chat_uses_chat_model(self) -> None:
        assert resolve_model("chat") == get_settings().claude_chat_model

    def test_extract_locator_uses_extract_model(self) -> None:
        assert resolve_model("extract_locator") == get_settings().claude_extract_model

    def test_unknown_purpose_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown AI purpose"):
            resolve_model("nonsense")


# ---------------------------------------------------------------------------
# Logging (Requirement 9.3)
# ---------------------------------------------------------------------------


class TestLogging:
    @mock_aws
    def test_call_emits_structured_log_with_usage_and_latency(self) -> None:
        _create_rate_limit_table()
        stream = _attach_json_logger()

        recording = _RecordingClient(
            _fake_response(
                input_tokens=100,
                output_tokens=25,
                cache_creation=10,
                cache_read=90,
            )
        )
        client = AiClient(client=recording)
        client.create_message(
            purpose="precheck",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=128,
        )

        record = _last_ai_log(stream)
        assert record["purpose"] == "precheck"
        assert record["model"] == get_settings().claude_precheck_model
        assert record["input_tokens"] == 100
        assert record["output_tokens"] == 25
        assert record["cache_creation_input_tokens"] == 10
        assert record["cache_read_input_tokens"] == 90
        assert "latency_ms" in record
        assert isinstance(record["latency_ms"], int | float)

    @mock_aws
    def test_call_logs_zero_usage_when_usage_absent(self) -> None:
        _create_rate_limit_table()
        stream = _attach_json_logger()

        no_usage = SimpleNamespace(content=[], usage=None)
        client = AiClient(client=_RecordingClient(no_usage))
        client.create_message(
            purpose="chat",
            messages=[{"role": "user", "content": "hi"}],
        )

        record = _last_ai_log(stream)
        assert record["input_tokens"] == 0
        assert record["output_tokens"] == 0
        assert record["cache_read_input_tokens"] == 0

    @mock_aws
    def test_model_can_be_overridden(self) -> None:
        _create_rate_limit_table()
        stream = _attach_json_logger()
        recording = _RecordingClient()
        client = AiClient(client=recording)
        client.create_message(
            purpose="chat",
            model="explicit-model-id",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert _last_ai_log(stream)["model"] == "explicit-model-id"
        assert recording.calls[0]["model"] == "explicit-model-id"


# ---------------------------------------------------------------------------
# Global rate limit (Requirement 2.4)
# ---------------------------------------------------------------------------


class TestGlobalRateLimit:
    @mock_aws
    def test_over_limit_raises_and_does_not_call_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _create_rate_limit_table()
        # Force the global AI-call limit down to 2 for a fast test.
        monkeypatch.setenv("RL_GLOBAL_AI_CALLS_PER_HOUR", "2")
        get_settings.cache_clear()

        recording = _RecordingClient()
        client = AiClient(client=recording)

        # Two calls are allowed.
        client.create_message(purpose="precheck", messages=[{"role": "user", "content": "a"}])
        client.create_message(purpose="precheck", messages=[{"role": "user", "content": "b"}])
        assert len(recording.calls) == 2

        # The third call exceeds the global limit: raises and never calls the model.
        with pytest.raises(AiRateLimitError) as exc_info:
            client.create_message(purpose="precheck", messages=[{"role": "user", "content": "c"}])
        assert exc_info.value.status_code == 429
        assert exc_info.value.code == "RATE_LIMIT_EXCEEDED"
        assert len(recording.calls) == 2, "model must not be called once over the limit"


# ---------------------------------------------------------------------------
# FakeClaude stub (Requirement 8.3)
# ---------------------------------------------------------------------------


class TestFakeClaude:
    def test_replays_recorded_fixture(self) -> None:
        """The committed sample fixture replays with its recorded content and usage."""
        fake = FakeClaude()
        response = fake.messages.create(
            model="claude-3-5-haiku-20241022",
            messages=[{"role": "user", "content": "Is this page a product review page?"}],
            max_tokens=256,
        )
        assert response.content[0].text.startswith("Yes")
        assert response.usage.input_tokens == 42
        assert response.usage.cache_read_input_tokens == 20

    def test_missing_fixture_raises_clear_error(self, tmp_path: Path) -> None:
        """A request with no fixture must raise MissingFixtureError mentioning make record-ai."""
        fake = FakeClaude(fixtures_dir=tmp_path)
        with pytest.raises(MissingFixtureError, match="make record-ai"):
            fake.messages.create(
                model="claude-3-5-haiku-20241022",
                messages=[{"role": "user", "content": "unseen request"}],
                max_tokens=64,
            )

    def test_record_mode_writes_fixture_without_fixture_present(self, tmp_path: Path) -> None:
        """With a record delegate, a missing fixture is fetched and written to disk.

        The real SDK returns a pydantic model with ``model_dump``; here the
        delegate returns a plain dict, which the recorder serialises directly.
        """
        recorded = {
            "id": "msg_rec",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 7,
                "output_tokens": 3,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
        delegate = _RecordingClient(recorded)
        fake = FakeClaude(fixtures_dir=tmp_path, record_delegate=delegate)

        messages = [{"role": "user", "content": "record me"}]
        response = fake.messages.create(
            model="claude-3-5-haiku-20241022", messages=messages, max_tokens=64
        )
        # Delegate was called and the response surfaced.
        assert len(delegate.calls) == 1
        assert response.usage.input_tokens == 7

        # A fixture file keyed by the request now exists and replays offline.
        key = request_key(
            model="claude-3-5-haiku-20241022",
            messages=messages,
            params={"max_tokens": 64},
        )
        assert (tmp_path / f"{key}.json").exists()

        replay = FakeClaude(fixtures_dir=tmp_path)
        replayed = replay.messages.create(
            model="claude-3-5-haiku-20241022", messages=messages, max_tokens=64
        )
        assert replayed.usage.input_tokens == 7

    @mock_aws
    def test_fakeclaude_integrates_with_ai_client(self) -> None:
        """AiClient wrapping FakeClaude replays a fixture end-to-end with no network."""
        _create_rate_limit_table()
        client = AiClient(client=FakeClaude())
        response = client.create_message(
            purpose="precheck",
            model="claude-3-5-haiku-20241022",
            messages=[{"role": "user", "content": "Is this page a product review page?"}],
            max_tokens=256,
        )
        assert response.content[0].text.startswith("Yes")


# ---------------------------------------------------------------------------
# Module-level factory
# ---------------------------------------------------------------------------


class TestFactory:
    def test_set_and_get_ai_client(self) -> None:
        stub = AiClient(client=FakeClaude())
        set_ai_client(stub)
        assert get_ai_client() is stub

    def test_reset_rebuilds_on_next_get(self) -> None:
        stub = AiClient(client=FakeClaude())
        set_ai_client(stub)
        reset_ai_client()
        assert ai_module._ai_client is None
