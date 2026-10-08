"""Unit tests for the Review Locator (``app.extraction.locator``), Task 4.1.

Covers the Claude call with the forced tool schema, the one repair retry, the
two error types, and chunk fan-out / merge:

- A valid single-chunk tool response parses into a ``LocatorResult``.
- An invalid first response followed by a valid repair succeeds (one retry).
- Two invalid responses raise ``LocatorUnavailable`` (retryable).
- A provider error or the global AI limit raises ``AIUnavailable`` (retryable).
- A multi-chunk page fans out and merges results by element reference.
- The prompt loads from the versioned file and warns that page content is data.
- The forced tool schema has no field that accepts review text.

The AI is stubbed: these tests build ``tool_use`` responses inline (allowed by
``testing.md`` for happy-path stubs and malformed output), so no network and no
recorded fixtures are needed here — rich recorded-response tests are Task 4.3.
"""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.ai import AiClient, reset_ai_client, set_ai_client
from app.core.config import get_settings
from app.extraction import locator
from app.extraction.errors import AIUnavailable, LocatorUnavailable
from app.extraction.models import CleanedPage, LocatorResult
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
    """Build an SDK-shaped message carrying a forced ``locator_result`` tool_use."""
    block = SimpleNamespace(type="tool_use", name="locator_result", input=tool_input)
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


def _valid_input(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    """A minimal valid tool input, with optional field overrides."""
    base: dict[str, Any] = {
        "has_reviews": True,
        "rating_scale": 5,
        "items": [{"item_ref": "e1", "text_ref": "e2", "rating_value": 5, "kind": "review"}],
        "selectors": {"item": "article.review"},
        "next_page": {"ref": "e9"},
        "reported_total": 100,
        "entity_hint": "Acme CRM",
        "confidence": "high",
    }
    base.update(overrides)
    return base


def _page(lines: list[str] | None = None, chunks: list[list[str]] | None = None) -> CleanedPage:
    lines = lines if lines is not None else ['e1 <div> "text"']
    return CleanedPage(
        lines=lines,
        lookup={"e1": "html > body > div"},
        chunks=chunks if chunks is not None else [lines],
        tokens=10,
    )


# ---------------------------------------------------------------------------
# Prompt + tool schema
# ---------------------------------------------------------------------------


class TestPromptAndSchema:
    def test_prompt_version_is_locator_v1(self) -> None:
        assert locator.PROMPT_VERSION == "locator_v1"

    def test_prompt_loads_and_declares_page_content_is_data(self) -> None:
        """Requirement 2.6: the prompt tells the model page content is data."""
        text = locator.load_prompt().lower()
        assert "data" in text
        assert "instruction" in text
        # Core steering rule: never supply/write review text.
        assert "never" in text

    def test_tool_schema_has_no_review_text_field(self) -> None:
        """Steering: the AI may point at elements but never supply review text."""
        schema = locator.tool_schema()
        assert schema["name"] == "locator_result"
        item_props = schema["input_schema"]["properties"]["items"]["items"]["properties"]
        # Only *_ref pointers and rating_value/kind — no raw text field.
        assert "text" not in item_props
        assert "text_ref" in item_props
        assert set(item_props) <= {
            "item_ref",
            "text_ref",
            "rating_value",
            "rating_ref",
            "date_ref",
            "author_ref",
            "title_ref",
            "kind",
        }

    def test_tool_schema_exposes_all_locator_result_fields(self) -> None:
        """Requirement 2.1: every LocatorResult field is representable in the tool."""
        props = set(locator.tool_schema()["input_schema"]["properties"])
        assert props == set(LocatorResult.model_fields)


# ---------------------------------------------------------------------------
# Single-chunk happy path
# ---------------------------------------------------------------------------


class TestSingleChunk:
    @mock_aws
    def test_valid_response_parses(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient([_tool_use_response(_valid_input())])
        _install(client)

        result = locator.locate(_page(), url="https://x.test/p", title="Reviews")

        assert isinstance(result, LocatorResult)
        assert result.has_reviews is True
        assert result.items[0].item_ref == "e1"
        assert result.selectors.item == "article.review"
        assert result.next_page.ref == "e9"
        assert len(client.calls) == 1

    @mock_aws
    def test_call_forces_the_locator_tool(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient([_tool_use_response(_valid_input())])
        _install(client)

        locator.locate(_page(), url="https://x.test/p", title="Reviews")

        call = client.calls[0]
        assert call["tool_choice"] == {"type": "tool", "name": "locator_result"}
        assert call["tools"][0]["name"] == "locator_result"
        # The system prompt is the versioned file; the page is framed as data.
        assert "data" in call["system"].lower()
        assert "<cleaned_page>" in call["messages"][0]["content"]


# ---------------------------------------------------------------------------
# Repair retry (Requirement 2.7)
# ---------------------------------------------------------------------------


class TestRepairRetry:
    @mock_aws
    def test_invalid_then_valid_succeeds_with_one_retry(self) -> None:
        _create_rate_limit_table()
        # First: bad rating value out of any sane shape (wrong type) → invalid.
        bad = _tool_use_response({"has_reviews": True, "rating_scale": "five"})
        good = _tool_use_response(_valid_input())
        client = _ScriptedClient([bad, good])
        _install(client)

        result = locator.locate(_page(), url="https://x.test/p", title="Reviews")

        assert result.has_reviews is True
        assert len(client.calls) == 2, "exactly one repair retry"
        # The repair turn quotes the failure and re-states the no-text rule.
        repair_text = client.calls[1]["messages"][-1]["content"].lower()
        assert "not valid" in repair_text
        assert "never review text" in repair_text

    @mock_aws
    def test_missing_tool_block_then_valid_succeeds(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient([_text_response(), _tool_use_response(_valid_input())])
        _install(client)

        result = locator.locate(_page(), url="https://x.test/p", title="Reviews")

        assert result.has_reviews is True
        assert len(client.calls) == 2

    @mock_aws
    def test_two_invalid_responses_raise_locator_unavailable(self) -> None:
        _create_rate_limit_table()
        bad = _tool_use_response({"has_reviews": True, "rating_scale": "five"})
        client = _ScriptedClient([bad, _tool_use_response({"items": "not-a-list"})])
        _install(client)

        with pytest.raises(LocatorUnavailable):
            locator.locate(_page(), url="https://x.test/p", title="Reviews")
        assert len(client.calls) == 2, "one call plus one repair, then give up"


# ---------------------------------------------------------------------------
# AI unavailable (Requirement 2.8)
# ---------------------------------------------------------------------------


class TestAIUnavailable:
    @mock_aws
    def test_provider_error_raises_ai_unavailable(self) -> None:
        _create_rate_limit_table()
        _install(_RaisingClient(RuntimeError("connection reset")))

        with pytest.raises(AIUnavailable):
            locator.locate(_page(), url="https://x.test/p", title="Reviews")

    @mock_aws
    def test_global_ai_limit_raises_ai_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _create_rate_limit_table()
        monkeypatch.setenv("RL_GLOBAL_AI_CALLS_PER_HOUR", "0")
        get_settings.cache_clear()
        # Response never consumed because the limit trips before the model runs.
        _install(_ScriptedClient([_tool_use_response(_valid_input())]))

        with pytest.raises(AIUnavailable):
            locator.locate(_page(), url="https://x.test/p", title="Reviews")


# ---------------------------------------------------------------------------
# Chunk fan-out and merge (Requirement 2.5)
# ---------------------------------------------------------------------------


class TestChunkMerge:
    @mock_aws
    def test_multi_chunk_merges_by_reference(self) -> None:
        _create_rate_limit_table()
        chunk_a = _tool_use_response(
            _valid_input(
                items=[{"item_ref": "e1", "kind": "review"}],
                excluded_refs=[{"ref": "e5", "kind": "qa"}],
                next_page={"ref": None},
                confidence="high",
                reported_total=100,
            )
        )
        chunk_b = _tool_use_response(
            _valid_input(
                # e1 duplicated across the overlap; e2 is new.
                items=[
                    {"item_ref": "e1", "kind": "review"},
                    {"item_ref": "e2", "kind": "review"},
                ],
                excluded_refs=[{"ref": "e5", "kind": "qa"}],
                next_page={"ref": "e9"},
                confidence="low",
                reported_total=100,
                selectors={"item": None, "text": ".body"},
            )
        )
        client = _ScriptedClient([chunk_a, chunk_b])
        _install(client)

        page = _page(chunks=[["e1 ..."], ["e2 ..."]])
        result = locator.locate(page, url="https://x.test/p", title="Reviews")

        # Items deduped by item_ref, chunk order preserved.
        assert [i.item_ref for i in result.items] == ["e1", "e2"]
        # Excluded deduped by ref.
        assert [e.ref for e in result.excluded_refs] == ["e5"]
        # First non-null next-page ref wins.
        assert result.next_page.ref == "e9"
        # Lowest confidence across chunks.
        assert result.confidence == "low"
        # Selectors merged field-by-field, first non-null each.
        assert result.selectors.item == "article.review"
        assert result.selectors.text == ".body"
        # Reported total is the page-level value.
        assert result.reported_total == 100
        assert len(client.calls) == 2

    @mock_aws
    def test_chunk_failure_propagates(self) -> None:
        _create_rate_limit_table()
        _install(_RaisingClient(RuntimeError("boom")))

        page = _page(chunks=[["e1 ..."], ["e2 ..."]])
        with pytest.raises(AIUnavailable):
            locator.locate(page, url="https://x.test/p", title="Reviews")
