"""Unit tests for the entity profiler (``app.worker.ai.profile``), task 4.1.

Covers the content-only Claude call with the forced ``entity_profile`` tool,
the low-confidence fallback (Requirement 4.2), and the AI-unavailable mapping:

- A confident, valid response parses into an ``EntityProfile`` unchanged.
- A ``low``-confidence response falls back to the page title (or upload name)
  for the name while keeping any derived category/description.
- An unparseable / missing-tool response falls back to the title and ``low``.
- A confident response with an empty name is repaired to the fallback label.
- A provider error or the global AI limit raises ``AIUnavailable`` (retryable).
- The prompt loads from the versioned file and states the content-only +
  data-not-instructions rules.
- The forced tool schema mirrors ``EntityProfile`` and has no review-text field.
- The prompt sends only content inputs (title, header, hint, reviews).

The AI is stubbed with inline ``tool_use`` responses (allowed by ``testing.md``
for happy-path stubs and malformed output); no network, no recorded fixtures.
"""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.ai import AiClient, reset_ai_client, set_ai_client
from app.core.config import get_settings
from app.extraction.errors import AIUnavailable
from app.extraction.models import VerifiedReview
from app.handlers.extraction_stage import CollectedReview
from app.worker.ai import profile
from app.worker.ai.profile import EntityProfile
from moto import mock_aws

from tests.support.dynamodb import ensure_rate_limit_table

_TABLE = "rate-limits"
_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# Fixtures / helpers
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


def _tool_use_response(tool_input: dict[str, Any]) -> SimpleNamespace:
    """Build an SDK-shaped message carrying a forced ``entity_profile`` tool_use."""
    block = SimpleNamespace(type="tool_use", name="entity_profile", input=tool_input)
    return SimpleNamespace(
        id="msg_test",
        content=[block],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def _text_response(text: str = "no tool here") -> SimpleNamespace:
    """A response with no tool_use block (malformed for a forced-tool call)."""
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(id="msg_test", content=[block], usage=None)


class _ScriptedClient:
    """Anthropic-like client returning queued responses in order; records calls."""

    def __init__(self, responses: list[Any]) -> None:  # noqa: ANN401
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("Scripted client ran out of responses")
        return self._responses.pop(0)


class _RaisingClient:
    """Anthropic-like client whose create() raises, simulating a provider error."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append(kwargs)
        raise self._exc


def _install(client: Any) -> None:  # noqa: ANN401
    set_ai_client(AiClient(client=client))


def _review(text: str) -> CollectedReview:
    return CollectedReview(review=VerifiedReview(text=text), source_page=1)


# ---------------------------------------------------------------------------
# Prompt + tool schema
# ---------------------------------------------------------------------------


class TestPromptAndSchema:
    def test_prompt_version_is_profile_v1(self) -> None:
        assert profile.PROMPT_VERSION == "profile_v1"

    def test_prompt_loads_and_states_content_only_and_data_rules(self) -> None:
        """Requirement 4.1: use only provided content; content is data."""
        text = profile.load_prompt().lower()
        assert "only" in text
        assert "outside knowledge" in text
        assert "instruction" in text
        # Steering: never supply review text.
        assert "never" in text

    def test_tool_schema_mirrors_entity_profile_fields(self) -> None:
        schema = profile.tool_schema()
        assert schema["name"] == "entity_profile"
        props = set(schema["input_schema"]["properties"])
        assert props == set(EntityProfile.model_fields)

    def test_tool_schema_has_no_review_text_field(self) -> None:
        """Steering: the profiler returns a derived description, never review text."""
        props = set(profile.tool_schema()["input_schema"]["properties"])
        assert props == {"name", "category", "description", "confidence"}


# ---------------------------------------------------------------------------
# Confident happy path
# ---------------------------------------------------------------------------


class TestConfident:
    @mock_aws
    def test_valid_confident_response_parses(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient(
            [
                _tool_use_response(
                    {
                        "name": "Acme CRM",
                        "category": "CRM software",
                        "description": "A customer relationship management tool.",
                        "confidence": "high",
                    }
                )
            ]
        )
        _install(client)

        result = profile.entity_profile(
            title="Acme CRM Reviews",
            header="Acme CRM",
            entity_hint="Acme CRM",
            reviews=[_review("Setup took an afternoon.")],
        )

        assert isinstance(result, EntityProfile)
        assert result.name == "Acme CRM"
        assert result.category == "CRM software"
        assert result.confidence == "high"
        assert len(client.calls) == 1

    @mock_aws
    def test_call_forces_the_tool_and_sends_only_content(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient([_tool_use_response({"name": "Acme CRM", "confidence": "high"})])
        _install(client)

        profile.entity_profile(
            title="Acme CRM Reviews",
            header="Acme CRM",
            entity_hint="Acme CRM",
            reviews=[_review("Great support.")],
        )

        call = client.calls[0]
        assert call["tool_choice"] == {"type": "tool", "name": "entity_profile"}
        assert call["tools"][0]["name"] == "entity_profile"
        # Content inputs present; framed as data.
        content = call["messages"][0]["content"]
        assert "<page_content>" in content
        assert "Acme CRM Reviews" in content  # title
        assert "Great support." in content  # review context
        assert "data" in call["system"].lower()


# ---------------------------------------------------------------------------
# Low-confidence fallback (Requirement 4.2)
# ---------------------------------------------------------------------------


class TestFallback:
    @mock_aws
    def test_low_confidence_falls_back_to_title(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient(
            [
                _tool_use_response(
                    {
                        "name": "something vague",
                        "category": "unclear",
                        "description": "A page with mixed content.",
                        "confidence": "low",
                    }
                )
            ]
        )
        _install(client)

        result = profile.entity_profile(
            title="Mystery Product Page",
            header="",
            entity_hint=None,
            reviews=[_review("ok")],
        )

        assert result.confidence == "low"
        # Name falls back to the page title; derived description is kept.
        assert result.name == "Mystery Product Page"
        assert result.description == "A page with mixed content."

    @mock_aws
    def test_low_confidence_prefers_upload_name_over_title(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient([_tool_use_response({"name": "x", "confidence": "low"})])
        _install(client)

        result = profile.entity_profile(
            title="untitled",
            upload_name="reviews-export.csv",
            reviews=[_review("ok")],
        )

        assert result.confidence == "low"
        assert result.name == "reviews-export.csv"

    @mock_aws
    def test_missing_tool_block_falls_back_low(self) -> None:
        _create_rate_limit_table()
        # Both the first and the repair response lack a tool block (task 4.4:
        # the fallback applies only after the single repair retry also fails).
        _install(_ScriptedClient([_text_response(), _text_response()]))

        result = profile.entity_profile(title="Page Title", reviews=[_review("ok")])

        assert result.confidence == "low"
        assert result.name == "Page Title"

    @mock_aws
    def test_invalid_tool_input_falls_back_low(self) -> None:
        _create_rate_limit_table()
        # Missing required 'name' / 'confidence' → schema validation fails on
        # both the first response and the repair retry (task 4.4).
        _install(
            _ScriptedClient(
                [
                    _tool_use_response({"category": "software"}),
                    _tool_use_response({"category": "software"}),
                ]
            )
        )

        result = profile.entity_profile(title="Page Title")

        assert result.confidence == "low"
        assert result.name == "Page Title"

    @mock_aws
    def test_confident_empty_name_is_repaired_to_fallback(self) -> None:
        _create_rate_limit_table()
        _install(_ScriptedClient([_tool_use_response({"name": "   ", "confidence": "high"})]))

        result = profile.entity_profile(title="Fallback Title")

        assert result.name == "Fallback Title"
        assert result.confidence == "low"


# ---------------------------------------------------------------------------
# AI unavailable
# ---------------------------------------------------------------------------


class TestAIUnavailable:
    @mock_aws
    def test_provider_error_raises_ai_unavailable(self) -> None:
        _create_rate_limit_table()
        _install(_RaisingClient(RuntimeError("connection reset")))

        with pytest.raises(AIUnavailable):
            profile.entity_profile(title="x", reviews=[_review("ok")])

    @mock_aws
    def test_global_ai_limit_raises_ai_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _create_rate_limit_table()
        monkeypatch.setenv("RL_GLOBAL_AI_CALLS_PER_HOUR", "0")
        get_settings.cache_clear()
        _install(_ScriptedClient([_tool_use_response({"name": "x", "confidence": "high"})]))

        with pytest.raises(AIUnavailable):
            profile.entity_profile(title="x")


# ---------------------------------------------------------------------------
# Schema-repair retry (task 4.4)
# ---------------------------------------------------------------------------


class _SequenceClient:
    """Anthropic-like client driven by a mixed queue of responses and exceptions.

    Each queued item is either an SDK-shaped response (returned) or an
    ``Exception`` instance (raised) on the corresponding call, in order. This
    lets a test interleave a normal response with an ``AIUnavailable`` raised on
    a specific (first or repair) call. Records every call.
    """

    def __init__(self, items: list[Any]) -> None:  # noqa: ANN401
        self._items = list(items)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append(kwargs)
        if not self._items:
            raise AssertionError("Sequence client ran out of items")
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestSchemaRepairRetry:
    """Task 4.4: one repair retry on a schema failure; AIUnavailable propagates."""

    @mock_aws
    def test_first_invalid_repair_valid_returns_repaired_result(self) -> None:
        """First response malformed, repair valid → repaired result, exactly 2 calls."""
        _create_rate_limit_table()
        client = _ScriptedClient(
            [
                # First response: missing required 'name'/'confidence' → invalid.
                _tool_use_response({"category": "software"}),
                # Repair response: valid and confident.
                _tool_use_response({"name": "Acme CRM", "category": "CRM", "confidence": "high"}),
            ]
        )
        _install(client)

        result = profile.entity_profile(title="Page Title", reviews=[_review("ok")])

        assert result.name == "Acme CRM"
        assert result.confidence == "high"
        assert len(client.calls) == 2

    @mock_aws
    def test_repair_call_appends_repair_prompt(self) -> None:
        """The second (repair) call resends content plus a repair instruction."""
        _create_rate_limit_table()
        client = _ScriptedClient(
            [
                _tool_use_response({"category": "software"}),
                _tool_use_response({"name": "Acme", "confidence": "high"}),
            ]
        )
        _install(client)

        profile.entity_profile(title="Page Title", reviews=[_review("great tool")])

        repair_content = client.calls[1]["messages"][0]["content"]
        # Original content is resent as context...
        assert "great tool" in repair_content
        # ...plus the repair instruction naming the schema requirement.
        assert "schema" in repair_content.lower()

    @mock_aws
    def test_both_invalid_applies_low_confidence_fallback(self) -> None:
        """Both responses malformed → low-confidence title fallback, exactly 2 calls."""
        _create_rate_limit_table()
        client = _ScriptedClient(
            [
                _tool_use_response({"category": "software"}),
                _text_response(),  # repair also malformed (no tool block)
            ]
        )
        _install(client)

        result = profile.entity_profile(title="Fallback Title", reviews=[_review("ok")])

        assert result.confidence == "low"
        assert result.name == "Fallback Title"
        assert len(client.calls) == 2

    @mock_aws
    def test_ai_unavailable_on_first_call_propagates_without_repair(self) -> None:
        """AIUnavailable on the first call → propagates, no repair retry, no fallback."""
        _create_rate_limit_table()
        # First call raises; a valid response is queued behind it that must
        # never be requested if AIUnavailable short-circuits the repair path.
        client = _SequenceClient(
            [
                AIUnavailable("provider down"),
                _tool_use_response({"name": "x", "confidence": "high"}),
            ]
        )
        _install(client)

        with pytest.raises(AIUnavailable):
            profile.entity_profile(title="Fallback Title", reviews=[_review("ok")])

        # Exactly one call: the repair path must not swallow AIUnavailable.
        assert len(client.calls) == 1

    @mock_aws
    def test_ai_unavailable_on_repair_call_propagates(self) -> None:
        """First call a schema failure, repair call hits AIUnavailable → propagates."""
        _create_rate_limit_table()
        client = _SequenceClient(
            [
                _tool_use_response({"category": "software"}),  # schema failure
                AIUnavailable("provider down on repair"),  # repair raises
            ]
        )
        _install(client)

        with pytest.raises(AIUnavailable):
            profile.entity_profile(title="Fallback Title", reviews=[_review("ok")])

        # Both calls were made: first (schema failure) then the repair (raises).
        assert len(client.calls) == 2
