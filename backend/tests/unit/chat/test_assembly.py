"""Unit tests for chat message assembly (``app.chat.assembly``), Task 1.3.

Covers the design's "Message layout" / "Prompt structure" rules:

- The cached ``<reviews>`` block: it is the first user turn's single content
  block and carries ``cache_control: {"type": "ephemeral"}`` (Requirement 7.3).
- History truncation: at most 6 Exchanges, each prior answer cut to 1,500 chars.
- Exclusion of Exchanges from older data versions (Requirements 2.6, 9.6).
- Exclusion of other conversations' Exchanges (Requirements 2.6, 9.6).
- The pre-check hint is included only when the pre-check flagged it.

Plus supporting checks: SCOPE placeholder filling in the system prompt, the
wrapped question turn, the truncation note surfacing, and the small S3 reader
(:func:`app.chat.assembly.load_recent_exchanges`) listing and filtering via a
faked ``storage.s3`` boundary.

Everything runs offline: no AWS, no AI call (assembly never calls the model).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.chat import assembly
from app.chat.assembly import (
    MAX_HISTORY_ANSWER_CHARS,
    MAX_HISTORY_EXCHANGES,
    PrecheckHint,
    PriorExchange,
    assemble_messages,
    load_recent_exchanges,
    select_history,
)
from app.chat.corpus import Corpus, CorpusReview, EntityProfile
from app.chat.prompts import LoadedPrompt
from app.storage import keys

_DS = "11111111-1111-1111-1111-111111111111"
_CONV = "conv-aaaa"
_OTHER_CONV = "conv-bbbb"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _corpus(
    *,
    version: int = 2,
    reviews: list[CorpusReview] | None = None,
    truncation_note: str | None = None,
) -> Corpus:
    if reviews is None:
        reviews = [
            CorpusReview(id="r_0001", text="Great support.", rating=5, date="2026-01-01"),
            CorpusReview(id="r_0002", text="Slow to load.", rating=2, date="2026-01-02"),
        ]
    return Corpus(
        dataset_id=_DS,
        version=version,
        entity=EntityProfile(name="Acme CRM", category="software", description="A CRM tool."),
        reviews=tuple(reviews),
        total_review_count=len(reviews),
        estimated_tokens=0,
        truncation_note=truncation_note,
    )


def _exchange(
    *,
    question: str = "What are the complaints?",
    answer: str = "Mostly slow support [r_0002].",
    conversation_id: str = _CONV,
    data_version: int = 2,
    asked_at: str = "2026-02-01T00:00:00Z",
) -> PriorExchange:
    return PriorExchange(
        question=question,
        answer=answer,
        conversation_id=conversation_id,
        data_version=data_version,
        asked_at=asked_at,
    )


# ---------------------------------------------------------------------------
# Cache-control placement on the reviews block
# ---------------------------------------------------------------------------


def test_corpus_block_is_first_turn_and_cache_marked() -> None:
    """The reviews block is the first user turn's only block, marked ephemeral."""
    result = assemble_messages(corpus=_corpus(), question="Hi", conversation_id=_CONV)

    first = result.messages[0]
    assert first["role"] == "user"
    assert isinstance(first["content"], list) and len(first["content"]) == 1
    block = first["content"][0]
    assert block["type"] == "text"
    assert block["cache_control"] == {"type": "ephemeral"}


def test_corpus_block_contains_reviews_as_json_lines() -> None:
    """Each review is a JSON line with exactly id, rating, date, text."""
    result = assemble_messages(corpus=_corpus(), question="Hi", conversation_id=_CONV)
    text = result.messages[0]["content"][0]["text"]

    assert "<reviews>" in text and "</reviews>" in text
    # The r_0001 line parses to a dict with just the four shown fields.
    line = next(ln for ln in text.splitlines() if '"r_0001"' in ln)
    parsed = json.loads(line)
    assert set(parsed) == {"id", "rating", "date", "text"}
    assert parsed["id"] == "r_0001"
    assert parsed["text"] == "Great support."


def test_only_corpus_turn_is_cached() -> None:
    """History and question turns are plain strings, so they are not cached."""
    history = [_exchange()]
    result = assemble_messages(
        corpus=_corpus(), question="Hi", conversation_id=_CONV, history=history
    )

    # Every turn after the first is a plain-string content turn (no cache_control).
    for turn in result.messages[1:]:
        assert isinstance(turn["content"], str)


def test_truncation_note_is_surfaced_in_block() -> None:
    """A Corpus truncation note appears in the cached block for the model."""
    note = "This answer covers the 1 most recent of 3 reviews; older reviews were omitted."
    result = assemble_messages(
        corpus=_corpus(truncation_note=note), question="Hi", conversation_id=_CONV
    )
    text = result.messages[0]["content"][0]["text"]
    assert note in text


# ---------------------------------------------------------------------------
# History truncation (max 6, answers to 1,500 chars)
# ---------------------------------------------------------------------------


def test_history_capped_at_six_exchanges() -> None:
    """At most 6 prior Exchanges are included, keeping the most recent."""
    history = [
        _exchange(question=f"q{i}", answer=f"a{i}", asked_at=f"2026-02-{i:02d}T00:00:00Z")
        for i in range(1, 11)  # 10 exchanges, days 01..10
    ]
    result = assemble_messages(
        corpus=_corpus(), question="latest", conversation_id=_CONV, history=history
    )

    assert result.included_history == MAX_HISTORY_EXCHANGES
    # 6 exchanges => 12 history turns, between the corpus turn and the question.
    history_turns = result.messages[1:-1]
    assert len(history_turns) == MAX_HISTORY_EXCHANGES * 2
    # The kept window is the most recent 6 (days 05..10), oldest-first.
    first_q = history_turns[0]
    assert first_q["role"] == "user" and first_q["content"] == "q5"


def test_history_answers_truncated_to_limit() -> None:
    """Each prior answer is truncated to MAX_HISTORY_ANSWER_CHARS."""
    long_answer = "z" * (MAX_HISTORY_ANSWER_CHARS + 500)
    result = assemble_messages(
        corpus=_corpus(),
        question="next",
        conversation_id=_CONV,
        history=[_exchange(answer=long_answer)],
    )

    assistant_turn = result.messages[2]  # corpus, user(q), assistant(a), question
    assert assistant_turn["role"] == "assistant"
    assert len(assistant_turn["content"]) == MAX_HISTORY_ANSWER_CHARS


def test_history_questions_are_not_truncated() -> None:
    """Questions (bounded to 1,000 chars upstream) are sent whole."""
    long_q = "q" * 1000
    result = assemble_messages(
        corpus=_corpus(),
        question="next",
        conversation_id=_CONV,
        history=[_exchange(question=long_q)],
    )
    user_turn = result.messages[1]
    assert user_turn["role"] == "user"
    assert user_turn["content"] == long_q


def test_history_turns_alternate_user_then_assistant() -> None:
    """Each Exchange renders as a user turn then an assistant turn, in order."""
    history = [
        _exchange(question="q1", answer="a1", asked_at="2026-02-01T00:00:00Z"),
        _exchange(question="q2", answer="a2", asked_at="2026-02-02T00:00:00Z"),
    ]
    result = assemble_messages(
        corpus=_corpus(), question="q3", conversation_id=_CONV, history=history
    )
    roles = [t["role"] for t in result.messages]
    # corpus(user), q1(user), a1(assistant), q2(user), a2(assistant), q3(user)
    assert roles == ["user", "user", "assistant", "user", "assistant", "user"]
    assert result.messages[1]["content"] == "q1"
    assert result.messages[4]["content"] == "a2"


# ---------------------------------------------------------------------------
# Exclusion of older-version and other-conversation Exchanges
# ---------------------------------------------------------------------------


def test_older_version_exchanges_excluded() -> None:
    """Exchanges answered against an older data version are never sent."""
    history = [
        _exchange(question="old", data_version=1),
        _exchange(question="current", data_version=2),
    ]
    result = assemble_messages(
        corpus=_corpus(version=2), question="now", conversation_id=_CONV, history=history
    )
    assert result.included_history == 1
    questions = [t["content"] for t in result.messages if t["role"] == "user"]
    assert "old" not in questions
    assert "current" in questions


def test_other_conversation_exchanges_excluded() -> None:
    """Exchanges from a different conversation are never sent as context."""
    history = [
        _exchange(question="mine", conversation_id=_CONV),
        _exchange(question="theirs", conversation_id=_OTHER_CONV),
    ]
    result = assemble_messages(
        corpus=_corpus(), question="now", conversation_id=_CONV, history=history
    )
    assert result.included_history == 1
    questions = [t["content"] for t in result.messages if t["role"] == "user"]
    assert "theirs" not in questions
    assert "mine" in questions


def test_select_history_combines_filters_and_window() -> None:
    """select_history applies conversation + version filters and the 6-window."""
    history = [
        _exchange(question="wrong-conv", conversation_id=_OTHER_CONV),
        _exchange(question="wrong-ver", data_version=1),
        *[
            _exchange(question=f"keep{i}", asked_at=f"2026-03-{i:02d}T00:00:00Z")
            for i in range(1, 9)  # 8 valid exchanges
        ],
    ]
    selected = select_history(history, conversation_id=_CONV, data_version=2)

    assert len(selected) == MAX_HISTORY_EXCHANGES
    assert [e.question for e in selected] == [f"keep{i}" for i in range(3, 9)]


# ---------------------------------------------------------------------------
# Pre-check hint inclusion only when flagged
# ---------------------------------------------------------------------------


def test_precheck_hint_included_when_flagged() -> None:
    """A flagged pre-check renders a <precheck> hint before the question."""
    result = assemble_messages(
        corpus=_corpus(),
        question="what's the weather?",
        conversation_id=_CONV,
        precheck=PrecheckHint(label="out_of_scope", category="weather"),
    )
    question_turn = result.messages[-1]["content"]
    assert "<precheck>likely out_of_scope: weather</precheck>" in question_turn
    assert "<question>what's the weather?</question>" in question_turn


def test_precheck_hint_absent_when_not_flagged() -> None:
    """With no pre-check hint, the question turn carries no <precheck> tag."""
    result = assemble_messages(
        corpus=_corpus(), question="what are the complaints?", conversation_id=_CONV
    )
    question_turn = result.messages[-1]["content"]
    assert "<precheck>" not in question_turn
    assert question_turn == "<question>what are the complaints?</question>"


def test_precheck_hint_without_category_omits_colon() -> None:
    """A flagged hint with no category renders just the label."""
    assert PrecheckHint(label="injection").render() == "<precheck>likely injection</precheck>"


# ---------------------------------------------------------------------------
# System prompt placeholder filling
# ---------------------------------------------------------------------------


def test_system_prompt_fills_scope_placeholders() -> None:
    """SCOPE placeholders are replaced from the corpus entity and dataset fields."""
    template = "Entity: {entity.name} ({entity.category}). Source: {platform} — {original_url}."
    result = assemble_messages(
        corpus=_corpus(),
        question="Hi",
        conversation_id=_CONV,
        platform="G2",
        original_url="https://g2.com/acme",
        prompt=LoadedPrompt(text=template, version="system_vtest"),
    )
    assert result.system == "Entity: Acme CRM (software). Source: G2 — https://g2.com/acme."
    assert result.prompt_version == "system_vtest"


def test_system_prompt_fallbacks_for_missing_values() -> None:
    """Missing platform/url/category become readable fallbacks, not raw tokens."""
    template = "{entity.name} / {entity.category} / {platform} / {original_url}"
    bare_corpus = Corpus(
        dataset_id=_DS,
        version=2,
        entity=EntityProfile(name="Acme CRM"),
        reviews=(),
        total_review_count=0,
    )
    result = assemble_messages(
        corpus=bare_corpus,
        question="Hi",
        conversation_id=_CONV,
        prompt=LoadedPrompt(text=template, version="system_vtest"),
    )
    assert "{" not in result.system
    assert result.system == "Acme CRM / unknown category / an uploaded file / (no source URL)"


# ---------------------------------------------------------------------------
# PriorExchange.from_dict
# ---------------------------------------------------------------------------


def test_prior_exchange_from_dict_reads_fields() -> None:
    """from_dict pulls the five assembly-relevant fields off a saved Exchange."""
    raw = {
        "id": "uuid",
        "dataset_id": _DS,
        "data_version": 3,
        "conversation_id": _CONV,
        "asked_at": "2026-02-01T00:00:00Z",
        "question": "Q",
        "answer": "A",
        "citations": ["r_0001"],
    }
    exchange = PriorExchange.from_dict(raw)
    assert exchange.question == "Q"
    assert exchange.answer == "A"
    assert exchange.conversation_id == _CONV
    assert exchange.data_version == 3
    assert exchange.asked_at == "2026-02-01T00:00:00Z"


# ---------------------------------------------------------------------------
# load_recent_exchanges (S3 reader)
# ---------------------------------------------------------------------------


@pytest.fixture
def s3_wiring(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake ``storage.s3`` listing/reading for the exchange reader.

    ``objects`` maps key -> JSON text. ``list_keys`` returns the sorted keys
    under the prefix (mirroring S3's lexicographic listing).
    """
    state: dict[str, Any] = {"objects": {}}

    def _list_keys(prefix: str, *, start_after: str = "") -> list[str]:
        matched = sorted(k for k in state["objects"] if k.startswith(prefix))
        if start_after:
            matched = [k for k in matched if k > start_after]
        return matched

    def _get_text(key: str, *, encoding: str = "utf-8") -> str:
        return str(state["objects"][key])

    monkeypatch.setattr(assembly.s3, "list_keys", _list_keys)
    monkeypatch.setattr(assembly.s3, "get_text", _get_text)
    return state


def _store_exchange(state: dict[str, Any], exchange: PriorExchange, *, exchange_id: str) -> None:
    key = keys.dataset_chat_exchange(_DS, exchange.asked_at, exchange_id)
    doc = {
        "question": exchange.question,
        "answer": exchange.answer,
        "conversation_id": exchange.conversation_id,
        "data_version": exchange.data_version,
        "asked_at": exchange.asked_at,
    }
    state["objects"][key] = json.dumps(doc)


def test_load_recent_exchanges_filters_and_orders(s3_wiring: dict[str, Any]) -> None:
    """The reader returns only this conversation's current-version Exchanges, oldest first."""
    _store_exchange(
        s3_wiring, _exchange(question="theirs", conversation_id=_OTHER_CONV), exchange_id="e1"
    )
    _store_exchange(
        s3_wiring,
        _exchange(question="old", data_version=1, asked_at="2026-02-02T00:00:00Z"),
        exchange_id="e2",
    )
    _store_exchange(
        s3_wiring,
        _exchange(question="mine-1", asked_at="2026-02-03T00:00:00Z"),
        exchange_id="e3",
    )
    _store_exchange(
        s3_wiring,
        _exchange(question="mine-2", asked_at="2026-02-04T00:00:00Z"),
        exchange_id="e4",
    )

    result = load_recent_exchanges(_DS, conversation_id=_CONV, data_version=2)

    assert [e.question for e in result] == ["mine-1", "mine-2"]


def test_load_recent_exchanges_empty_when_none(s3_wiring: dict[str, Any]) -> None:
    """No stored exchanges yields an empty context list."""
    result = load_recent_exchanges(_DS, conversation_id=_CONV, data_version=2)
    assert result == []
