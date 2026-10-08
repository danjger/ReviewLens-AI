"""Unit tests for theme extraction (``app.worker.ai.themes``), task 4.3.

Covers the single Claude call with the forced ``extract_themes`` tool, the
corpus-id check (the heart of this task), the 8-theme cap, the empty-examples
choice, the omit-with-warning path, and the AI-unavailable mapping:

- A valid themes result is parsed and returned with no warnings.
- More than 8 themes are capped to the 8 highest-mention themes.
- ``example_ids`` are filtered to ids that exist in the corpus; invented or
  out-of-range ids are dropped, survivors deduped in first-seen order.
- A theme whose ``example_ids`` are all invalid is kept, without examples.
- Unparseable / invalid output → themes omitted with a warning.
- A provider error or the global AI limit raises ``AIUnavailable`` (retryable).
- The prompt loads from the versioned file and states the content-only +
  data-not-instructions + never-supply-text rules.
- The forced tool schema has no review-text field.
- The ``themes`` purpose resolves to the extract model (config, not a literal).

The AI is stubbed with inline ``tool_use`` responses (allowed by ``testing.md``
for happy-path stubs and malformed output); no network, no recorded fixtures.
"""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.ai import AiClient, reset_ai_client, resolve_model, set_ai_client
from app.core.config import get_settings
from app.extraction.errors import AIUnavailable
from app.extraction.models import VerifiedReview
from app.handlers.extraction_stage import CollectedReview
from app.worker.ai import themes
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


def _tool_use_response(themes_payload: list[dict[str, Any]]) -> SimpleNamespace:
    """Build an SDK-shaped message carrying a forced ``extract_themes`` tool_use."""
    block = SimpleNamespace(
        type="tool_use",
        name="extract_themes",
        input={"themes": themes_payload},
    )
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


def _review(text: str, rating: float | None = None) -> CollectedReview:
    return CollectedReview(review=VerifiedReview(text=text, rating=rating), source_page=1)


def _reviews(n: int) -> list[CollectedReview]:
    return [_review(f"Review {i}") for i in range(n)]


# ---------------------------------------------------------------------------
# Prompt + tool schema + model resolution
# ---------------------------------------------------------------------------


class TestPromptSchemaAndModel:
    def test_prompt_version_is_themes_v1(self) -> None:
        assert themes.PROMPT_VERSION == "themes_v1"

    def test_prompt_states_content_only_data_and_never_text_rules(self) -> None:
        """Requirement 5.3: only provided content; data not instructions; never supply text."""
        text = themes.load_prompt().lower()
        assert "only" in text
        assert "outside knowledge" in text
        assert "instruction" in text
        assert "never" in text

    def test_tool_schema_has_no_review_text_field(self) -> None:
        """Steering: the model returns labels, counts, leans, and ids, never review text."""
        schema = themes.tool_schema()
        assert schema["name"] == "extract_themes"
        item_props = set(schema["input_schema"]["properties"]["themes"]["items"]["properties"])
        assert item_props == {"label", "mentions", "lean", "example_ids"}

    def test_themes_purpose_resolves_to_extract_model(self) -> None:
        """Steering: model IDs come from config via a purpose, never a literal."""
        assert resolve_model("themes") == get_settings().claude_extract_model


# ---------------------------------------------------------------------------
# Valid result (happy path)
# ---------------------------------------------------------------------------


class TestValidResult:
    @mock_aws
    def test_valid_themes_parsed_with_no_warnings(self) -> None:
        _create_rate_limit_table()
        reviews = _reviews(3)
        _install(
            _ScriptedClient(
                [
                    _tool_use_response(
                        [
                            {
                                "label": "Customer support",
                                "mentions": 2,
                                "lean": "negative",
                                "example_ids": ["0", "2"],
                            },
                            {
                                "label": "Ease of use",
                                "mentions": 1,
                                "lean": "positive",
                                "example_ids": ["1"],
                            },
                        ]
                    )
                ]
            )
        )

        result, warnings = themes.extract_themes(reviews)

        assert warnings == []
        assert [t.label for t in result] == ["Customer support", "Ease of use"]
        assert result[0].mentions == 2
        assert result[0].lean == "negative"
        assert result[0].example_ids == ["0", "2"]
        assert result[1].example_ids == ["1"]

    @mock_aws
    def test_empty_reviews_makes_no_call(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient([])
        _install(client)

        result, warnings = themes.extract_themes([])

        assert result == []
        assert warnings == []
        assert len(client.calls) == 0

    @mock_aws
    def test_call_forces_tool_and_sends_reviews_as_data(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient(
            [_tool_use_response([{"label": "Value", "mentions": 1, "lean": "positive"}])]
        )
        _install(client)

        themes.extract_themes([_review("Great value for money")])

        call = client.calls[0]
        assert call["tool_choice"] == {"type": "tool", "name": "extract_themes"}
        assert call["tools"][0]["name"] == "extract_themes"
        content = call["messages"][0]["content"]
        assert "<reviews>" in content
        assert "[0]" in content
        assert "Great value for money" in content
        assert "data" in call["system"].lower()


# ---------------------------------------------------------------------------
# 8-theme cap
# ---------------------------------------------------------------------------


class TestCap:
    @mock_aws
    def test_more_than_eight_themes_capped_to_highest_mention_eight(self) -> None:
        _create_rate_limit_table()
        reviews = _reviews(1)
        # 10 themes with distinct mention counts 1..10; expect the 8 highest kept.
        payload = [
            {"label": f"Theme {i}", "mentions": i, "lean": "neutral", "example_ids": []}
            for i in range(1, 11)
        ]
        _install(_ScriptedClient([_tool_use_response(payload)]))

        result, warnings = themes.extract_themes(reviews)

        assert warnings == []
        assert len(result) == 8
        kept_mentions = sorted(t.mentions for t in result)
        # The two lowest (1 and 2) are dropped; 3..10 survive.
        assert kept_mentions == [3, 4, 5, 6, 7, 8, 9, 10]

    @mock_aws
    def test_exactly_eight_themes_all_kept_in_order(self) -> None:
        _create_rate_limit_table()
        reviews = _reviews(1)
        payload = [
            {"label": f"Theme {i}", "mentions": 1, "lean": "neutral", "example_ids": []}
            for i in range(8)
        ]
        _install(_ScriptedClient([_tool_use_response(payload)]))

        result, warnings = themes.extract_themes(reviews)

        assert warnings == []
        assert [t.label for t in result] == [f"Theme {i}" for i in range(8)]


# ---------------------------------------------------------------------------
# Corpus-id check (the heart of task 4.3)
# ---------------------------------------------------------------------------


class TestCorpusIdCheck:
    @mock_aws
    def test_nonexistent_ids_are_dropped(self) -> None:
        _create_rate_limit_table()
        reviews = _reviews(3)  # valid ids: "0", "1", "2"
        _install(
            _ScriptedClient(
                [
                    _tool_use_response(
                        [
                            {
                                "label": "Support",
                                "mentions": 2,
                                "lean": "negative",
                                # "7" and "abc" do not exist; "1" does.
                                "example_ids": ["1", "7", "abc"],
                            }
                        ]
                    )
                ]
            )
        )

        result, warnings = themes.extract_themes(reviews)

        assert warnings == []
        assert result[0].example_ids == ["1"]

    @mock_aws
    def test_valid_ids_deduped_preserving_first_seen_order(self) -> None:
        _create_rate_limit_table()
        reviews = _reviews(3)
        _install(
            _ScriptedClient(
                [
                    _tool_use_response(
                        [
                            {
                                "label": "Support",
                                "mentions": 3,
                                "lean": "neutral",
                                "example_ids": ["2", "0", "2", "0"],
                            }
                        ]
                    )
                ]
            )
        )

        result, _warnings = themes.extract_themes(reviews)

        assert result[0].example_ids == ["2", "0"]

    @mock_aws
    def test_theme_with_no_valid_ids_is_kept_without_examples(self) -> None:
        """Documented choice: a theme whose ids are all invalid is kept, empty-examples."""
        _create_rate_limit_table()
        reviews = _reviews(2)  # valid ids: "0", "1"
        _install(
            _ScriptedClient(
                [
                    _tool_use_response(
                        [
                            {
                                "label": "Phantom theme",
                                "mentions": 1,
                                "lean": "positive",
                                "example_ids": ["99", "nope"],
                            }
                        ]
                    )
                ]
            )
        )

        result, warnings = themes.extract_themes(reviews)

        assert warnings == []
        assert len(result) == 1
        assert result[0].label == "Phantom theme"
        assert result[0].example_ids == []


# ---------------------------------------------------------------------------
# Omit-with-warning (design Error Handling)
# ---------------------------------------------------------------------------


class TestOmitWithWarning:
    @mock_aws
    def test_no_tool_block_omits_themes_with_warning(self) -> None:
        _create_rate_limit_table()
        # First and repair responses both lack a tool block (task 4.4): themes
        # are omitted only after the single repair retry also fails.
        _install(_ScriptedClient([_text_response(), _text_response()]))

        result, warnings = themes.extract_themes(_reviews(2))

        assert result == []
        assert warnings == [themes.THEMES_OMITTED_WARNING]

    @mock_aws
    def test_invalid_schema_omits_themes_with_warning(self) -> None:
        _create_rate_limit_table()
        # "lean" is not one of the allowed values → schema validation fails on
        # both the first response and the repair retry (task 4.4).
        _install(
            _ScriptedClient(
                [
                    _tool_use_response([{"label": "X", "mentions": 1, "lean": "ecstatic"}]),
                    _tool_use_response([{"label": "X", "mentions": 1, "lean": "ecstatic"}]),
                ]
            )
        )

        result, warnings = themes.extract_themes(_reviews(2))

        assert result == []
        assert warnings == [themes.THEMES_OMITTED_WARNING]


# ---------------------------------------------------------------------------
# AI unavailable
# ---------------------------------------------------------------------------


class TestAIUnavailable:
    @mock_aws
    def test_provider_error_raises_ai_unavailable(self) -> None:
        _create_rate_limit_table()
        _install(_RaisingClient(RuntimeError("connection reset")))

        with pytest.raises(AIUnavailable):
            themes.extract_themes(_reviews(1))

    @mock_aws
    def test_global_ai_limit_raises_ai_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _create_rate_limit_table()
        monkeypatch.setenv("RL_GLOBAL_AI_CALLS_PER_HOUR", "0")
        get_settings.cache_clear()
        _install(
            _ScriptedClient(
                [_tool_use_response([{"label": "X", "mentions": 1, "lean": "neutral"}])]
            )
        )

        with pytest.raises(AIUnavailable):
            themes.extract_themes(_reviews(1))


# ---------------------------------------------------------------------------
# Schema-repair retry (task 4.4)
# ---------------------------------------------------------------------------


class _SequenceClient:
    """Anthropic-like client driven by a mixed queue of responses and exceptions.

    Each queued item is either an SDK-shaped response (returned) or an
    ``Exception`` instance (raised) on the corresponding call, in order. Lets a
    test interleave a normal response with an ``AIUnavailable`` raised on a
    specific (first or repair) call. Records every call.
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
    def test_first_invalid_repair_valid_returns_repaired_themes(self) -> None:
        """First response malformed, repair valid → themes returned, exactly 2 calls."""
        _create_rate_limit_table()
        reviews = _reviews(2)  # valid corpus ids "0", "1"
        client = _ScriptedClient(
            [
                # First response: invalid lean → schema validation fails.
                _tool_use_response([{"label": "X", "mentions": 1, "lean": "ecstatic"}]),
                # Repair response: valid.
                _tool_use_response(
                    [{"label": "Support", "mentions": 2, "lean": "negative", "example_ids": ["0"]}]
                ),
            ]
        )
        _install(client)

        result, warnings = themes.extract_themes(reviews)

        assert warnings == []
        assert [t.label for t in result] == ["Support"]
        assert result[0].example_ids == ["0"]
        assert len(client.calls) == 2

    @mock_aws
    def test_repair_call_appends_repair_prompt(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient(
            [
                _text_response(),
                _tool_use_response([{"label": "Value", "mentions": 1, "lean": "positive"}]),
            ]
        )
        _install(client)

        themes.extract_themes([_review("Great value for money")])

        repair_content = client.calls[1]["messages"][0]["content"]
        assert "Great value for money" in repair_content  # corpus resent
        assert "schema" in repair_content.lower()  # repair instruction appended

    @mock_aws
    def test_both_invalid_omits_themes_with_warning(self) -> None:
        """Both responses malformed → themes omitted with a warning, exactly 2 calls."""
        _create_rate_limit_table()
        client = _ScriptedClient([_text_response(), _text_response()])
        _install(client)

        result, warnings = themes.extract_themes(_reviews(2))

        assert result == []
        assert warnings == [themes.THEMES_OMITTED_WARNING]
        assert len(client.calls) == 2

    @mock_aws
    def test_ai_unavailable_on_first_call_propagates_without_repair(self) -> None:
        _create_rate_limit_table()
        client = _SequenceClient(
            [
                AIUnavailable("provider down"),
                _tool_use_response([{"label": "X", "mentions": 1, "lean": "neutral"}]),
            ]
        )
        _install(client)

        with pytest.raises(AIUnavailable):
            themes.extract_themes(_reviews(1))

        assert len(client.calls) == 1  # no repair retry on AIUnavailable

    @mock_aws
    def test_ai_unavailable_on_repair_call_propagates(self) -> None:
        _create_rate_limit_table()
        client = _SequenceClient(
            [
                _text_response(),  # schema failure
                AIUnavailable("provider down on repair"),  # repair raises
            ]
        )
        _install(client)

        with pytest.raises(AIUnavailable):
            themes.extract_themes(_reviews(1))

        assert len(client.calls) == 2
