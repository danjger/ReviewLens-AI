"""Unit tests for sentiment classification (``app.worker.ai.sentiment``), task 4.2.

Covers the batched Claude call with the forced ``classify_sentiment`` tool, the
rating-based fallback (design Error Handling), the no-rating default, batching
boundaries, and the AI-unavailable mapping:

- A full valid batch maps labels back onto reviews by id.
- 50 reviews → one batch; 51+ → multiple batches (one AI call each).
- A batch whose output cannot be parsed falls back to rating-based labels.
- A batch result missing a review's id falls back to the rating rule for it.
- The rating rule: >=4 positive, ==3 neutral, <=2 negative.
- A review with no rating and no valid AI label defaults to neutral.
- A provider error or the global AI limit raises ``AIUnavailable`` (retryable).
- The prompt loads from the versioned file and states the content-only +
  data-not-instructions + rating-is-a-hint rules.
- The forced tool schema has no review-text field.

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
from app.worker.ai import sentiment
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


def _tool_use_response(results: list[dict[str, Any]]) -> SimpleNamespace:
    """Build an SDK-shaped message carrying a forced ``classify_sentiment`` tool_use."""
    block = SimpleNamespace(
        type="tool_use",
        name="classify_sentiment",
        input={"results": results},
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


# ---------------------------------------------------------------------------
# Prompt + tool schema
# ---------------------------------------------------------------------------


class TestPromptAndSchema:
    def test_prompt_version_is_sentiment_v1(self) -> None:
        assert sentiment.PROMPT_VERSION == "sentiment_v1"

    def test_prompt_states_content_only_data_and_hint_rules(self) -> None:
        """Requirement 5.2: only provided content; data not instructions; rating is a hint."""
        text = sentiment.load_prompt().lower()
        assert "only" in text
        assert "outside knowledge" in text
        assert "instruction" in text
        assert "hint" in text
        # Steering: never supply review text.
        assert "never" in text

    def test_tool_schema_has_no_review_text_field(self) -> None:
        """Steering: the model returns ids and labels, never review text."""
        schema = sentiment.tool_schema()
        assert schema["name"] == "classify_sentiment"
        item_props = set(schema["input_schema"]["properties"]["results"]["items"]["properties"])
        assert item_props == {"id", "sentiment"}


# ---------------------------------------------------------------------------
# Rating-based fallback rule (design Error Handling)
# ---------------------------------------------------------------------------


class TestRatingSentiment:
    @pytest.mark.parametrize(
        ("rating", "expected"),
        [
            (5, "positive"),
            (4, "positive"),
            (4.5, "positive"),
            (3, "neutral"),
            (3.5, "neutral"),
            (2, "negative"),
            (1, "negative"),
            (0, "negative"),
            (None, "neutral"),
        ],
    )
    def test_rating_rule(self, rating: float | None, expected: str) -> None:
        assert sentiment.rating_sentiment(rating) == expected


# ---------------------------------------------------------------------------
# Valid batch (happy path)
# ---------------------------------------------------------------------------


class TestValidBatch:
    @mock_aws
    def test_full_valid_batch_maps_labels_by_id(self) -> None:
        _create_rate_limit_table()
        reviews = [
            _review("Loved it", rating=5),
            _review("It was fine", rating=3),
            _review("Terrible", rating=1),
        ]
        client = _ScriptedClient(
            [
                _tool_use_response(
                    [
                        {"id": 0, "sentiment": "positive"},
                        {"id": 1, "sentiment": "neutral"},
                        {"id": 2, "sentiment": "negative"},
                    ]
                )
            ]
        )
        _install(client)

        labels = sentiment.classify_sentiment(reviews)

        assert labels == ["positive", "neutral", "negative"]
        assert len(client.calls) == 1

    @mock_aws
    def test_ai_label_overrides_rating_hint(self) -> None:
        """Rating is a hint, not the final answer: a 5-star sarcastic review is negative."""
        _create_rate_limit_table()
        reviews = [_review("Oh *wonderful*, broke on day one", rating=5)]
        _install(_ScriptedClient([_tool_use_response([{"id": 0, "sentiment": "negative"}])]))

        labels = sentiment.classify_sentiment(reviews)

        assert labels == ["negative"]

    @mock_aws
    def test_call_forces_tool_and_sends_reviews_as_data(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient([_tool_use_response([{"id": 0, "sentiment": "positive"}])])
        _install(client)

        sentiment.classify_sentiment([_review("Great value", rating=5)])

        call = client.calls[0]
        assert call["tool_choice"] == {"type": "tool", "name": "classify_sentiment"}
        assert call["tools"][0]["name"] == "classify_sentiment"
        content = call["messages"][0]["content"]
        assert "<reviews>" in content
        assert "Great value" in content
        assert "data" in call["system"].lower()

    @mock_aws
    def test_empty_reviews_makes_no_call(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient([])
        _install(client)

        assert sentiment.classify_sentiment([]) == []
        assert len(client.calls) == 0


# ---------------------------------------------------------------------------
# Batching boundary
# ---------------------------------------------------------------------------


class TestBatching:
    @mock_aws
    def test_fifty_reviews_is_one_batch(self) -> None:
        _create_rate_limit_table()
        reviews = [_review(f"Review {i}", rating=5) for i in range(50)]
        client = _ScriptedClient(
            [_tool_use_response([{"id": i, "sentiment": "positive"} for i in range(50)])]
        )
        _install(client)

        labels = sentiment.classify_sentiment(reviews)

        assert len(labels) == 50
        assert len(client.calls) == 1

    @mock_aws
    def test_fifty_one_reviews_is_two_batches(self) -> None:
        _create_rate_limit_table()
        reviews = [_review(f"Review {i}", rating=5) for i in range(51)]
        client = _ScriptedClient(
            [
                _tool_use_response([{"id": i, "sentiment": "positive"} for i in range(50)]),
                _tool_use_response([{"id": 0, "sentiment": "neutral"}]),
            ]
        )
        _install(client)

        labels = sentiment.classify_sentiment(reviews)

        assert len(labels) == 51
        assert len(client.calls) == 2
        # Second batch's single review got the second call's label.
        assert labels[50] == "neutral"

    @mock_aws
    def test_batch_local_ids_restart_per_batch(self) -> None:
        """The id in batch 2's result is batch-local (0-based), mapped onto position 50."""
        _create_rate_limit_table()
        reviews = [_review(f"Review {i}", rating=1) for i in range(52)]
        client = _ScriptedClient(
            [
                _tool_use_response([{"id": i, "sentiment": "negative"} for i in range(50)]),
                _tool_use_response(
                    [{"id": 0, "sentiment": "positive"}, {"id": 1, "sentiment": "neutral"}]
                ),
            ]
        )
        _install(client)

        labels = sentiment.classify_sentiment(reviews)

        assert labels[50] == "positive"
        assert labels[51] == "neutral"


# ---------------------------------------------------------------------------
# Rating-based fallback paths
# ---------------------------------------------------------------------------


class TestFallback:
    @mock_aws
    def test_unparseable_batch_falls_back_to_rating(self) -> None:
        _create_rate_limit_table()
        reviews = [
            _review("Loved it", rating=5),
            _review("Meh", rating=3),
            _review("Awful", rating=1),
        ]
        # First response and the repair retry both unparseable (task 4.4): the
        # batch falls back to rating-based labels only after both fail.
        _install(_ScriptedClient([_text_response(), _text_response()]))

        labels = sentiment.classify_sentiment(reviews)

        assert labels == ["positive", "neutral", "negative"]

    @mock_aws
    def test_missing_id_in_result_falls_back_for_that_review(self) -> None:
        _create_rate_limit_table()
        reviews = [
            _review("Loved it", rating=2),  # AI will label this
            _review("No label from AI", rating=5),  # omitted → rating fallback → positive
        ]
        # AI only returns a label for id 0 (and even contradicts the rating).
        _install(_ScriptedClient([_tool_use_response([{"id": 0, "sentiment": "positive"}])]))

        labels = sentiment.classify_sentiment(reviews)

        assert labels[0] == "positive"  # AI label used
        assert labels[1] == "positive"  # rating fallback (5 → positive)

    @mock_aws
    def test_out_of_range_id_is_ignored_and_review_falls_back(self) -> None:
        _create_rate_limit_table()
        reviews = [_review("Only review", rating=1)]
        # AI returns an id that is not in the batch → ignored → rating fallback.
        _install(_ScriptedClient([_tool_use_response([{"id": 99, "sentiment": "positive"}])]))

        labels = sentiment.classify_sentiment(reviews)

        assert labels == ["negative"]  # rating 1 → negative

    @mock_aws
    def test_no_rating_no_ai_label_defaults_neutral(self) -> None:
        _create_rate_limit_table()
        reviews = [_review("Unrated and unlabelled", rating=None)]
        # Unparseable batch on both the first and repair call → fallback; no
        # rating → neutral default (task 4.4).
        _install(_ScriptedClient([_text_response(), _text_response()]))

        labels = sentiment.classify_sentiment(reviews)

        assert labels == ["neutral"]


# ---------------------------------------------------------------------------
# AI unavailable
# ---------------------------------------------------------------------------


class TestAIUnavailable:
    @mock_aws
    def test_provider_error_raises_ai_unavailable(self) -> None:
        _create_rate_limit_table()
        _install(_RaisingClient(RuntimeError("connection reset")))

        with pytest.raises(AIUnavailable):
            sentiment.classify_sentiment([_review("ok", rating=5)])

    @mock_aws
    def test_global_ai_limit_raises_ai_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _create_rate_limit_table()
        monkeypatch.setenv("RL_GLOBAL_AI_CALLS_PER_HOUR", "0")
        get_settings.cache_clear()
        _install(_ScriptedClient([_tool_use_response([{"id": 0, "sentiment": "positive"}])]))

        with pytest.raises(AIUnavailable):
            sentiment.classify_sentiment([_review("ok", rating=5)])


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
    """Task 4.4: one repair retry on a schema failure; AIUnavailable propagates.

    The repair retry is per-batch; a single-review batch exercises it with one
    call per attempt.
    """

    @mock_aws
    def test_first_invalid_repair_valid_returns_repaired_labels(self) -> None:
        """First batch response malformed, repair valid → AI labels, exactly 2 calls."""
        _create_rate_limit_table()
        # rating=1 would fall back to "negative"; a valid repair must override it.
        reviews = [_review("Sarcastic 1-star that is actually praise", rating=1)]
        client = _ScriptedClient(
            [
                _text_response(),  # first response: no tool block → invalid
                _tool_use_response([{"id": 0, "sentiment": "positive"}]),  # repair valid
            ]
        )
        _install(client)

        labels = sentiment.classify_sentiment(reviews)

        assert labels == ["positive"]  # AI repair label, not the rating fallback
        assert len(client.calls) == 2

    @mock_aws
    def test_repair_call_appends_repair_prompt(self) -> None:
        _create_rate_limit_table()
        client = _ScriptedClient(
            [
                _text_response(),
                _tool_use_response([{"id": 0, "sentiment": "positive"}]),
            ]
        )
        _install(client)

        sentiment.classify_sentiment([_review("Great value", rating=5)])

        repair_content = client.calls[1]["messages"][0]["content"]
        assert "Great value" in repair_content  # original batch resent
        assert "schema" in repair_content.lower()  # repair instruction appended

    @mock_aws
    def test_both_invalid_applies_rating_fallback(self) -> None:
        """Both responses malformed → rating-based fallback, exactly 2 calls."""
        _create_rate_limit_table()
        reviews = [
            _review("Loved it", rating=5),
            _review("Meh", rating=3),
            _review("Awful", rating=1),
        ]
        client = _ScriptedClient([_text_response(), _text_response()])
        _install(client)

        labels = sentiment.classify_sentiment(reviews)

        assert labels == ["positive", "neutral", "negative"]  # rating rule
        assert len(client.calls) == 2

    @mock_aws
    def test_ai_unavailable_on_first_call_propagates_without_repair(self) -> None:
        _create_rate_limit_table()
        client = _SequenceClient(
            [
                AIUnavailable("provider down"),
                _tool_use_response([{"id": 0, "sentiment": "positive"}]),
            ]
        )
        _install(client)

        with pytest.raises(AIUnavailable):
            sentiment.classify_sentiment([_review("ok", rating=5)])

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
            sentiment.classify_sentiment([_review("ok", rating=5)])

        assert len(client.calls) == 2
