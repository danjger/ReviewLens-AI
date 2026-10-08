"""Message assembly for the guardrailed chat (Task 1.3).

This module turns a question — plus the dataset's :class:`~app.chat.corpus.Corpus`
and the shared Q&A history — into the exact Anthropic message structure the
instrumented client (:class:`app.core.ai.AiClient`) expects. It **only
assembles** the request; it never calls the model. The streaming endpoint
(Task 4.2) passes the assembled ``system`` and ``messages`` to
``get_ai_client().create_message(purpose="chat", ...)``.

The layout follows the design's "Message layout" and "Prompt structure":

1. ``system`` — the loaded system prompt (``app.chat.prompts``, Task 1.1) with
   its SCOPE placeholders filled from the Corpus's entity and the dataset's
   platform / original URL. The prompt text is the sole source of the scope
   contract; nothing here weakens it.
2. The **first user turn** carries one content block: the ``<reviews>`` block
   (one JSON line ``{id, rating, date, text}`` per review) plus the entity
   profile and any Corpus truncation note. That block is marked
   ``cache_control: {"type": "ephemeral"}`` so the whole Corpus is prompt-cached
   (Requirement 7.3) — repeat questions on the same dataset re-read the cached
   prefix instead of re-sending ~100k–150k tokens.
3. The **last 6 Exchanges** with the *same* ``conversation_id`` and *from the
   current data version only*, as alternating user/assistant turns (each answer
   truncated to :data:`MAX_HISTORY_ANSWER_CHARS`). Exchanges from older versions
   and from other conversations are never sent (Requirements 2.6, 9.6), so
   answers grounded in stale data cannot carry forward and one visitor's
   conversation cannot leak into another's context.
4. The **current question**, wrapped as ``<question>…</question>``, with an
   optional ``<precheck>likely {label}: {category}</precheck>`` hint included
   *only* when the pre-check flagged it (Task 2 runs the pre-check; this module
   just places the hint when given one).

Review text placed in the ``<reviews>`` block is copied verbatim from the Corpus
(which review-analysis built from page elements). The model is told, by the
system prompt, that this content is data and not instructions (Requirement 4.1);
this module keeps that boundary by never mixing review text into the system
prompt or the question turn.

The history-selection logic (:func:`select_history`) is pure and offline so the
filtering rules are unit-testable without S3. :func:`load_recent_exchanges` is a
small S3 reader that lists a dataset's saved Exchange objects and returns the
ones this assembly needs; no earlier spec provides an Exchange reader, and the
Exchange objects it parses are the ones Task 4.3 will write (design "Data
Models": ``datasets/{id}/chat/{iso_ts}-{uuid}.json``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.chat.corpus import Corpus, CorpusReview
from app.chat.prompts import LoadedPrompt, load_system_prompt
from app.storage import keys, s3

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

# ---------------------------------------------------------------------------
# Tunables (design "Message layout")
# ---------------------------------------------------------------------------

#: How many prior Exchanges are sent to the model as conversation context. The
#: design fixes this at "the last 6 Exchanges"; keeping the window small bounds
#: prompt growth and keeps follow-ups coherent without resending the whole log.
MAX_HISTORY_EXCHANGES = 6

#: Each prior answer is truncated to this many characters before being sent back
#: as an assistant turn (design: "answers truncated to 1,500 characters each"),
#: so a long earlier answer cannot dominate the context window. Questions are
#: short (<=1,000 chars by Requirement 6.1) and are sent whole.
MAX_HISTORY_ANSWER_CHARS = 1500


# ---------------------------------------------------------------------------
# Prior Exchange (assembly input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriorExchange:
    """One saved Q&A Exchange, reduced to what message assembly needs.

    This is the assembly-facing view of the Exchange object Task 4.3 writes to
    ``datasets/{id}/chat/{iso_ts}-{uuid}.json`` (design "Data Models"). Only the
    fields that decide inclusion (``conversation_id``, ``data_version``,
    ``asked_at`` for ordering) and the two turns that go into the prompt
    (``question``, ``answer``) are kept, so the selection rules can be tested
    without constructing a full Exchange.

    :ivar question: The analyst's original question (sent whole as a user turn).
    :ivar answer: The assistant's saved answer (truncated to
        :data:`MAX_HISTORY_ANSWER_CHARS` before being sent as an assistant turn).
    :ivar conversation_id: The asking browser tab's random conversation ID. Only
        Exchanges whose ID matches the current question's are sent as context.
    :ivar data_version: The data version this Exchange was answered against. Only
        Exchanges from the current ``active_version`` are sent (Requirement 9.6).
    :ivar asked_at: The ISO-8601 ask timestamp, used only to order history
        oldest-first so the alternating turns read chronologically.
    """

    question: str
    answer: str
    conversation_id: str
    data_version: int
    asked_at: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PriorExchange:
        """Build a :class:`PriorExchange` from a saved Exchange document."""
        return cls(
            question=str(raw.get("question", "")),
            answer=str(raw.get("answer", "")),
            conversation_id=str(raw.get("conversation_id", "")),
            data_version=int(raw.get("data_version", 0)),
            asked_at=str(raw.get("asked_at", "")),
        )


# ---------------------------------------------------------------------------
# Pre-check hint (assembly input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrecheckHint:
    """A scope pre-check result worth passing to the main model as a hint.

    The pre-check (Task 2) runs in parallel and classifies the question. Only a
    flagged result — ``out_of_scope`` or ``injection`` — becomes a hint; an
    ``in_scope`` result (or a timed-out pre-check) adds nothing, so the system
    prompt alone decides. The hint never answers; it only nudges.

    :ivar label: The flagged label, e.g. ``"out_of_scope"`` or ``"injection"``.
    :ivar category: An optional sub-category, e.g. ``"weather"``; omitted from
        the rendered hint when empty.
    """

    label: str
    category: str | None = None

    def render(self) -> str:
        """Render the hint tag, e.g. ``<precheck>likely out_of_scope: weather</precheck>``."""
        detail = f"{self.label}: {self.category}" if self.category else self.label
        return f"<precheck>likely {detail}</precheck>"


# ---------------------------------------------------------------------------
# Assembled result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssembledMessages:
    """The ready-to-send request for ``AiClient.create_message(purpose="chat")``.

    :ivar system: The system prompt text with SCOPE placeholders filled.
    :ivar messages: The Anthropic ``messages`` list — the cached corpus turn,
        the selected history turns, then the wrapped question turn.
    :ivar prompt_version: The system prompt's version (for the saved Exchange's
        ``prompt_version`` field).
    :ivar included_history: How many prior Exchanges were included as context
        (0..:data:`MAX_HISTORY_EXCHANGES`), surfaced for logging/tests.
    """

    system: str
    messages: list[dict[str, Any]]
    prompt_version: str
    included_history: int


# ---------------------------------------------------------------------------
# System prompt placeholder filling
# ---------------------------------------------------------------------------


def _fill_system_prompt(
    template: str,
    *,
    corpus: Corpus,
    platform: str | None,
    original_url: str | None,
) -> str:
    """Fill the system prompt's SCOPE placeholders.

    The ``system_v1.md`` template uses ``{entity.name}``, ``{entity.category}``,
    ``{platform}``, and ``{original_url}``. Those are not valid ``str.format``
    field names (the dot makes ``entity`` an attribute lookup), and the template
    also contains literal braces in its numbered list, so a plain ``.format``
    would misfire. We therefore substitute the four known tokens explicitly and
    leave every other brace untouched.

    Missing values get readable fallbacks so the prompt never shows a raw
    placeholder: an unknown category becomes ``"unknown category"``, an uploaded
    dataset with no platform becomes ``"an uploaded file"``, and a missing URL
    becomes ``"(no source URL)"``.
    """
    replacements = {
        "{entity.name}": corpus.entity.name or "this entity",
        "{entity.category}": corpus.entity.category or "unknown category",
        "{platform}": platform or "an uploaded file",
        "{original_url}": original_url or "(no source URL)",
    }
    filled = template
    for token, value in replacements.items():
        filled = filled.replace(token, value)
    return filled


# ---------------------------------------------------------------------------
# Cached corpus content block
# ---------------------------------------------------------------------------


def _review_line(review: CorpusReview) -> str:
    """Render one review as a compact JSON line ``{id, rating, date, text}``.

    Only the four shown fields go into the line (matching the design and the
    Corpus's own token estimate). ``ensure_ascii=False`` keeps non-ASCII review
    text readable and compact; ``sort_keys`` makes the output deterministic for
    tests and for stable prompt caching.
    """
    payload: dict[str, Any] = {
        "id": review.id,
        "rating": review.rating,
        "date": review.date,
        "text": review.text,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _corpus_block_text(corpus: Corpus) -> str:
    """Build the first user turn's text: the ``<reviews>`` block plus the profile.

    The entity profile is placed first so the model has the subject in view, then
    the reviews as JSON lines inside a ``<reviews>`` fence the system prompt
    refers to. A Corpus truncation note (design "Error Handling") is surfaced
    verbatim so the model knows it is seeing the N most recent of M reviews.
    """
    entity = corpus.entity
    header_lines = [
        "<entity>",
        f"name: {entity.name}",
        f"category: {entity.category or 'unknown'}",
    ]
    if entity.description:
        header_lines.append(f"description: {entity.description}")
    header_lines.append("</entity>")

    review_lines = [_review_line(review) for review in corpus.reviews]
    block = [
        "\n".join(header_lines),
        "<reviews>",
        "\n".join(review_lines),
        "</reviews>",
    ]
    if corpus.truncation_note:
        block.append(f"<note>{corpus.truncation_note}</note>")
    return "\n".join(block)


def _corpus_turn(corpus: Corpus) -> dict[str, Any]:
    """Build the first user turn carrying the cache-marked corpus content block.

    The single content block is marked ``cache_control: {"type": "ephemeral"}``
    so the whole Corpus prefix is prompt-cached (Requirement 7.3). This is the
    only block that is cached; history and the question turn that follow it are
    not, so they can change per request without invalidating the cached Corpus.
    """
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": _corpus_block_text(corpus),
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


# ---------------------------------------------------------------------------
# History selection (pure)
# ---------------------------------------------------------------------------


def select_history(
    exchanges: Iterable[PriorExchange],
    *,
    conversation_id: str,
    data_version: int,
) -> list[PriorExchange]:
    """Pick the Exchanges that may be sent as conversation context.

    Applies the design's three rules (Requirements 2.6, 9.6):

    1. Keep only Exchanges whose ``conversation_id`` matches the asking tab's —
       other conversations' Exchanges are never sent, even though they appear in
       the shared history for everyone.
    2. Keep only Exchanges from *data_version* (the dataset's current
       ``active_version``) — Exchanges answered against older data are never
       sent, so stale answers cannot carry into a new answer.
    3. Keep at most the most recent :data:`MAX_HISTORY_EXCHANGES`, ordered
       oldest-first by ``asked_at`` so the resulting turns read chronologically.

    :param exchanges: Candidate Exchanges (any order; typically a dataset's
        recent saved Exchanges).
    :param conversation_id: The asking browser tab's conversation ID.
    :param data_version: The dataset's current active version.
    :returns: The selected Exchanges, oldest first.
    """
    matching = [
        exchange
        for exchange in exchanges
        if exchange.conversation_id == conversation_id and exchange.data_version == data_version
    ]
    matching.sort(key=lambda exchange: exchange.asked_at)
    # Keep the most recent window, preserving oldest-first order.
    return matching[-MAX_HISTORY_EXCHANGES:]


def _history_turns(history: Sequence[PriorExchange]) -> list[dict[str, Any]]:
    """Render selected history as alternating user/assistant turns.

    Each Exchange becomes a user turn (the original question, sent whole) and an
    assistant turn (the answer truncated to :data:`MAX_HISTORY_ANSWER_CHARS`).
    Truncation guards the context window against a very long earlier answer; the
    full answer is always available in the saved history for the UI.
    """
    turns: list[dict[str, Any]] = []
    for exchange in history:
        turns.append({"role": "user", "content": exchange.question})
        turns.append({"role": "assistant", "content": exchange.answer[:MAX_HISTORY_ANSWER_CHARS]})
    return turns


# ---------------------------------------------------------------------------
# Question turn
# ---------------------------------------------------------------------------


def _question_turn(question: str, precheck: PrecheckHint | None) -> dict[str, Any]:
    """Build the final user turn: the wrapped question and an optional hint.

    The question is wrapped as ``<question>…</question>`` so the model can tell
    the current ask apart from the review data and the prior turns. A pre-check
    hint is prepended on its own line *only* when one was flagged; an unflagged
    or absent pre-check adds nothing, leaving the system prompt to decide.
    """
    parts: list[str] = []
    if precheck is not None:
        parts.append(precheck.render())
    parts.append(f"<question>{question}</question>")
    return {"role": "user", "content": "\n".join(parts)}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def assemble_messages(
    *,
    corpus: Corpus,
    question: str,
    conversation_id: str,
    history: Iterable[PriorExchange] = (),
    platform: str | None = None,
    original_url: str | None = None,
    precheck: PrecheckHint | None = None,
    prompt: LoadedPrompt | None = None,
) -> AssembledMessages:
    """Assemble the chat request (system + messages) for the instrumented client.

    Builds, in order: the placeholder-filled system prompt, the cache-marked
    corpus turn, the selected history turns (same conversation, current version,
    last :data:`MAX_HISTORY_EXCHANGES`), and the wrapped question turn with an
    optional pre-check hint. The result is handed straight to
    ``AiClient.create_message(purpose="chat", system=..., messages=...)`` — this
    function performs no I/O and makes no model call.

    :param corpus: The dataset's :class:`~app.chat.corpus.Corpus` for the current
        ``active_version`` (from :func:`app.chat.corpus.load_corpus`). History is
        filtered to ``corpus.version`` so only current-version Exchanges are sent.
    :param question: The analyst's question (already length-validated upstream).
    :param conversation_id: The asking browser tab's conversation ID; history is
        filtered to this ID.
    :param history: Candidate prior Exchanges (any order). Defaults to none.
    :param platform: The dataset's source platform, for the SCOPE line. ``None``
        for an uploaded dataset.
    :param original_url: The dataset's source URL, for the SCOPE line.
    :param precheck: A flagged :class:`PrecheckHint`, or ``None`` when the
        pre-check said in-scope, timed out, or was not run.
    :param prompt: An override system prompt (tests); defaults to the current
        ``system_v1`` via :func:`app.chat.prompts.load_system_prompt`.
    :returns: The :class:`AssembledMessages`.
    """
    loaded = prompt if prompt is not None else load_system_prompt()
    system = _fill_system_prompt(
        loaded.text,
        corpus=corpus,
        platform=platform,
        original_url=original_url,
    )

    selected = select_history(
        history,
        conversation_id=conversation_id,
        data_version=corpus.version,
    )

    messages: list[dict[str, Any]] = [_corpus_turn(corpus)]
    messages.extend(_history_turns(selected))
    messages.append(_question_turn(question, precheck))

    return AssembledMessages(
        system=system,
        messages=messages,
        prompt_version=loaded.version,
        included_history=len(selected),
    )


# ---------------------------------------------------------------------------
# Exchange reader (small S3 helper)
# ---------------------------------------------------------------------------


def load_recent_exchanges(
    dataset_id: str,
    *,
    conversation_id: str,
    data_version: int,
    scan_limit: int = 200,
) -> list[PriorExchange]:
    """Load the Exchanges that may be sent as context for this conversation.

    Lists a dataset's saved Exchange objects under ``datasets/{id}/chat/`` (key
    prefix from :mod:`app.storage.keys`), reads the most recent *scan_limit*
    objects, and returns the ones that pass :func:`select_history` for
    *conversation_id* and *data_version* — at most
    :data:`MAX_HISTORY_EXCHANGES`, oldest first.

    No earlier spec provides an Exchange reader, so this is the small reader
    Task 1.3 is allowed to add. It parses the Exchange shape Task 4.3 writes
    (``question``, ``answer``, ``conversation_id``, ``data_version``,
    ``asked_at``); any object missing those fields degrades gracefully via
    :meth:`PriorExchange.from_dict`'s defaults. The listing is chronological
    (the key's ISO-8601 timestamp prefix sorts by time), so taking the tail
    bounds the read even for a long shared history; the per-conversation,
    per-version filter then narrows it to the context window.

    :param dataset_id: The dataset whose chat history to read.
    :param conversation_id: The asking tab's conversation ID (context filter).
    :param data_version: The dataset's current ``active_version`` (context
        filter — only this version's Exchanges are returned).
    :param scan_limit: How many of the most recent Exchange objects to read
        before filtering. The shared log mixes conversations, so this is set
        well above :data:`MAX_HISTORY_EXCHANGES`; it bounds S3 reads without
        loading an unbounded history.
    :returns: The selected :class:`PriorExchange` list, oldest first.
    """
    all_keys = s3.list_keys(keys.dataset_chat_prefix(dataset_id))
    recent_keys = all_keys[-scan_limit:] if scan_limit > 0 else all_keys

    exchanges: list[PriorExchange] = []
    for key in recent_keys:
        raw: dict[str, Any] = json.loads(s3.get_text(key))
        exchanges.append(PriorExchange.from_dict(raw))

    return select_history(
        exchanges,
        conversation_id=conversation_id,
        data_version=data_version,
    )
