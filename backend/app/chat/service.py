"""Chat service – the streaming FastAPI application (guardrailed-chat Task 4).

This is the stateless FastAPI app that serves the guardrailed Q&A chat. It runs
**unchanged** in both compute modes (steering: "same code, both compute modes"):

- **Lambda mode** (first deployment): the ``backend`` image runs this app
  behind the AWS Lambda Web Adapter with ``AWS_LWA_INVOKE_MODE=response_stream``
  on a Function URL (auth type ``NONE``) fronted by CloudFront. The CDK wiring
  lives in ``infra`` (``ApiStack.chatFunction`` → streaming Function URL, Edge
  routes ``/api/chat/*`` to it and adds the ``X-Origin-Verify`` header). The
  LWA ``cmd`` override serves ``app.chat:app`` — i.e. this app, re-exported
  from the package ``__init__`` (see the module layout note below).
- **Container mode**: the same image runs ``uvicorn app.chat:app`` behind the
  load balancer. ``docker-compose`` runs it as the ``chat`` service on 8001.

Request flow (Task 4.2 — design "Architecture")
-----------------------------------------------
Every ``POST /api/chat/datasets/{id}`` request runs, in order:

1. **Origin guard** (``OriginGuardMiddleware``, before this handler) + **rate
   limit** (per-IP + global ``questions`` counter). Both reused from
   ``platform-foundation``; a 429 is a normal HTTP response, not a mid-stream
   event, so it is raised before any streaming begins.
2. **Availability guard**: load the dataset; if it has no ``active_version`` or
   is archived, respond ``409 CHAT_UNAVAILABLE`` (Requirement 1.2/1.3; the
   client disables the input in those states). A dataset whose refresh is in
   flight or whose last refresh failed is still available — the chat answers
   from the current ``active_version`` (Requirement 1.1).
3. **Length validation**: reject an empty/whitespace-only or over-1,000-char
   question with ``422`` (design "Error Handling"; the client guards too).
4. **Parallel load + pre-check**: start the scope pre-check on a background
   thread (``precheck.run_in_background``) and, meanwhile, load the Corpus
   (``corpus.load_corpus`` for the active version) and the conversation's recent
   Exchanges (``assembly.load_recent_exchanges``); then collect the pre-check
   within its 1.5-second timeout (``precheck.await_result``) → an optional hint.
5. **Assemble** the request (``assembly.assemble_messages``) and make the
   **streaming Claude call** through the instrumented client
   (``get_ai_client().stream_message(purpose="chat", ...)``; the model id comes
   from config). Each streamed chunk is forwarded as an SSE ``token`` event
   (Requirement 7.1).
6. **Post-process** the full answer in sequence: ``process_citations`` →
   ``enforce_no_prompt_leak`` → ``tag_scope`` (Task 3). The shown and saved
   answer is the cleaned/replaced text.
7. Emit the terminal SSE **``done``** event with the final Exchange payload.
8. A Claude error/timeout mid-stream emits an SSE **``error``** event; the
   partial answer already streamed stays shown and **nothing is saved** (design
   "Error Handling").

Task 4.3 seam
-------------
Task 4.2 builds the ``done`` payload — the fully assembled Exchange content with
``saved: False`` — and streams it. The actual S3 ``put`` of the Exchange object,
the HMAC signature, the ``chat.exchange.saved`` event publish, and flipping
``saved`` to ``True`` are Task 4.3. The single seam is
:func:`_persist_exchange`, called just before the ``done`` event: it currently
returns the payload with ``saved=False`` unchanged, and Task 4.3 replaces its
body with the save + sign + publish without touching the request flow.

Module layout note (why this file, not ``app/chat.py``)
-------------------------------------------------------
The chat domain owns the ``app.chat`` **package** (``prompts``, ``corpus``,
``assembly``, ``precheck``, ``postprocess`` from Tasks 1–3). A sibling
``app/chat.py`` module cannot coexist with it, so the FastAPI app lives here in
``app/chat/service.py`` and is **re-exported from the package** ``__init__`` so
``app.chat:app`` — the entry point ``docker-compose``, the backend ``Dockerfile``
override, and the CDK ``ChatFunction`` all use — resolves to this app.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.chat import assembly, corpus, postprocess, precheck, signing, sse
from app.core.ai import get_ai_client
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.errors import (
    AppValidationError,
    ConflictError,
    RateLimitError,
    register_error_handlers,
)
from app.core.health import liveness, readiness
from app.core.origin_guard import OriginGuardMiddleware
from app.core.rate_limit import check_rate_limit, get_client_ip
from app.db.models import Dataset
from app.events.publisher import publish_event
from app.storage import keys, s3

logger = logging.getLogger(__name__)

#: Rate-limit action label for chat questions (per-IP + global ``questions``
#: counters, capped by ``RL_QUESTIONS_PER_IP_HOUR`` / ``RL_GLOBAL_AI_CALLS_...``).
_QUESTIONS_ACTION = "questions"

#: Attribute carrying the parsed ``Retry-After`` seconds on a ``RateLimitError``
#: so the dedicated handler can emit the header (mirrors ``ingestion.api``).
_RETRY_AFTER_ATTR = "retry_after_seconds"

#: Error code for the availability guard (design "Error Handling": "No
#: ``active_version``, or archived → 409 ``CHAT_UNAVAILABLE``").
CHAT_UNAVAILABLE_CODE = "CHAT_UNAVAILABLE"

#: Error code for a rejected question (empty / whitespace-only / too long).
INVALID_QUESTION_CODE = "INVALID_QUESTION"

#: Maximum question length accepted server-side (Requirement 6.1: "up to 1,000
#: characters"). The client guards too; this is the server backstop (422).
MAX_QUESTION_CHARS = 1000

#: Max tokens for the streamed answer. Chat answers are prose, not long
#: documents; this bounds a runaway generation without truncating normal
#: answers.
_CHAT_MAX_TOKENS = 2048

#: SSE error code emitted when the model call fails or times out mid-stream
#: (design "Error Handling": "Claude error or timeout mid-stream → SSE ``error``").
_STREAM_ERROR_CODE = "ANSWER_INTERRUPTED"

#: EventBridge detail-type published after a successful save so the push
#: consumer fans the new Exchange out to every browser viewing the dataset
#: (Requirement 5.7). The event body carries IDs only (steering: "queue/event
#: bodies carry IDs, payloads live in S3/DB").
_EXCHANGE_SAVED_EVENT = "chat.exchange.saved"

#: Content type for the saved Exchange S3 object.
_EXCHANGE_CONTENT_TYPE = "application/json"


app = FastAPI(title="ReviewLens Chat")

# CloudFront origin verification: reject requests that bypass CloudFront. In
# Lambda mode (Function URL auth NONE) this is the only thing blocking direct
# calls. A no-op in local development where the secret is empty.
app.add_middleware(OriginGuardMiddleware)

# Shared error envelope for AppError / validation / unexpected errors.
register_error_handlers(app)


@app.exception_handler(RateLimitError)
async def _handle_rate_limited(request: Request, exc: RateLimitError) -> JSONResponse:
    """Return the ``429`` envelope with a numeric ``Retry-After`` header.

    The shared ``AppError`` handler returns the body but cannot know the retry
    delay; this handler adds the ``Retry-After`` header (design "Error
    Handling": "429 with ``Retry-After``; the input shows when the analyst can
    ask again") while keeping the same ``{"error": {"code", "message"}}`` body.
    Mirrors the API service's handler in ``app.ingestion.api``.
    """
    headers: dict[str, str] = {}
    retry_after = getattr(exc, _RETRY_AFTER_ATTR, None)
    if retry_after is None:
        retry_after = _retry_after_seconds(exc.message)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """Body of ``POST /api/chat/datasets/{id}``.

    ``conversation_id`` is the per-browser-tab random id that scopes follow-up
    context (design: "each browser tab generates a random ``conversation_id``").
    When omitted (or blank) the request is treated as a fresh conversation: a
    random id is minted so the Exchange is still saved into the shared history
    (Requirement 2.6), it just has no prior turns to resolve against.

    ``question`` length is validated in the handler (not here) so that an
    empty/whitespace-only or over-length question yields the chat's own
    ``422 INVALID_QUESTION`` envelope rather than a generic Pydantic error, and
    so trimming/validation live in one place with the availability guard.
    """

    question: str = Field(...)
    conversation_id: str | None = None


# ---------------------------------------------------------------------------
# Resolved dataset state (availability guard output)
# ---------------------------------------------------------------------------


class _DatasetState:
    """The dataset fields the chat flow needs, read once under a db session.

    Carrying these out of the session (rather than a live ORM object) keeps the
    db session short — opened only for the availability guard — while the longer
    Corpus load, pre-check, and streaming call run without holding a connection.
    """

    __slots__ = ("active_version", "archived", "original_url", "platform")

    def __init__(
        self,
        *,
        active_version: int | None,
        archived: bool,
        platform: str | None,
        original_url: str | None,
    ) -> None:
        self.active_version = active_version
        self.archived = archived
        self.platform = platform
        self.original_url = original_url


# ---------------------------------------------------------------------------
# Streaming endpoint
# ---------------------------------------------------------------------------


@app.post("/api/chat/datasets/{dataset_id}")
async def chat(dataset_id: str, body: ChatRequest, request: Request) -> StreamingResponse:
    """Answer a question about *dataset_id*, streaming the answer as SSE.

    Runs the full request flow described in the module docstring. The guards
    that can refuse the request — rate limit, availability (``409``), and
    length validation (``422``) — run **before** any streaming so they are
    ordinary HTTP error responses. Once those pass, the response is a
    ``text/event-stream`` of ``token`` events, a terminal ``done`` event with
    the Exchange payload, or an ``error`` event if the model call fails
    mid-stream.
    """
    settings = get_settings()

    # (1) Rate limit (per-IP + global). Before streaming so a 429 is a normal
    # HTTP response. ``get_client_ip`` returns None off-CloudFront (local/tests):
    # only the global counter runs then; the raw IP is hashed inside the limiter.
    client_ip = get_client_ip(request)
    _enforce_question_rate_limit(client_ip, settings.rl_questions_per_ip_hour)

    # (3) Length validation (done here, before the availability DB read is even
    # needed, so a malformed question is cheapest to reject). Trims for the
    # emptiness check but sends the original text to the model.
    question = _validate_question(body.question)

    # (2) Availability guard: 409 when there is no active version or the dataset
    # is archived. A refresh in flight / a failed refresh still answers from the
    # current active_version (Requirement 1.1), so only those two states block.
    state = _assert_chat_available(_load_dataset_state(dataset_id))
    active_version = state.active_version
    assert active_version is not None  # noqa: S101 - guaranteed by _assert_chat_available

    conversation_id = _resolve_conversation_id(body.conversation_id)

    # (4) Parallel load + pre-check. Start the pre-check on a background thread,
    # then load the Corpus and this conversation's recent Exchanges while it
    # runs, then collect the pre-check within its 1.5s timeout.
    loaded_corpus = corpus.load_corpus(dataset_id, active_version)
    precheck_task = precheck.run_in_background(
        question,
        entity_name=loaded_corpus.entity.name,
        entity_category=loaded_corpus.entity.category or "",
        platform=state.platform or "",
    )
    history = assembly.load_recent_exchanges(
        dataset_id,
        conversation_id=conversation_id,
        data_version=active_version,
    )
    precheck_result = precheck.await_result(precheck_task)
    hint = precheck_result.to_hint() if precheck_result is not None else None

    # (5) Assemble the request for the instrumented client.
    assembled = assembly.assemble_messages(
        corpus=loaded_corpus,
        question=question,
        conversation_id=conversation_id,
        history=history,
        platform=state.platform,
        original_url=state.original_url,
        precheck=hint,
    )

    asked_at = _utc_now_iso()

    async def event_stream() -> AsyncIterator[str]:
        """Yield the SSE events: tokens, then done — or an error mid-stream."""
        chunks: list[str] = []
        stream_result = get_ai_client().stream_message(
            purpose="chat",
            system=assembled.system,
            messages=assembled.messages,
            max_tokens=_CHAT_MAX_TOKENS,
        )
        try:
            for chunk in stream_result.text_chunks:
                chunks.append(chunk)
                yield sse.token_event(chunk)
        except Exception as exc:  # noqa: BLE001 - any provider error → SSE error
            # (8) Mid-stream failure: surface an error event; the partial answer
            # already streamed stays shown and nothing is saved (design "Error
            # Handling"). The full traceback is logged, not sent to the browser.
            logger.warning(
                "chat_stream_failed dataset_id=%s error=%s",
                dataset_id,
                exc,
                extra={"event": "chat_stream_failed", "dataset_id": dataset_id},
            )
            yield sse.error_event(_STREAM_ERROR_CODE, "Answer interrupted — retry")
            return

        raw_answer = "".join(chunks)

        # (6) Post-process in sequence: citations → prompt-leak → scope tag.
        citation_result = postprocess.process_citations(raw_answer, loaded_corpus)
        leak_result = postprocess.enforce_no_prompt_leak(raw_answer)
        scope_result = postprocess.tag_scope(
            leak_result.answer,
            precheck=precheck_result,
            leaked=leak_result.leaked,
        )

        # (7) Build the Exchange and emit the done event.
        exchange = _build_exchange(
            dataset_id=dataset_id,
            data_version=active_version,
            conversation_id=conversation_id,
            asked_at=asked_at,
            question=question,
            answer=scope_result.answer,
            citation_result=citation_result,
            scope_result=scope_result,
            precheck_result=precheck_result,
            prompt_version=assembled.prompt_version,
            usage=stream_result.usage.as_dict(),
        )
        exchange = _persist_exchange(exchange)
        yield sse.done_event(exchange)

    return StreamingResponse(
        event_stream(),
        media_type=sse.SSE_MEDIA_TYPE,
        headers=sse.SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# Health probes (exempt from the origin guard — see core.health)
# ---------------------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """Liveness probe – returns 200 if the process is alive."""
    return liveness()


@app.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    """Readiness probe – checks database and S3 connectivity."""
    return readiness(
        database_url=os.environ.get("DATABASE_URL"),
        s3_bucket=os.environ.get("S3_BUCKET"),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
    )


# ---------------------------------------------------------------------------
# Availability guard
# ---------------------------------------------------------------------------


def _load_dataset_state(dataset_id: str) -> _DatasetState | None:
    """Read the dataset's availability + scope fields, or ``None`` if unknown.

    Opens a short db session (reused from ``platform-foundation``'s
    ``core.db``), reads only ``active_version``, ``archived_at``, ``platform``,
    and ``original_url``, and returns them detached so the session closes before
    the longer Corpus/stream work. Returns ``None`` when the id is unknown, which
    the guard maps to the same ``409 CHAT_UNAVAILABLE`` as "no active version":
    the chat input is simply unavailable, and the no-sign-in app never leaks
    whether a given id exists.
    """
    with session_scope() as session:
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:
            return None
        return _DatasetState(
            active_version=dataset.active_version,
            archived=dataset.archived_at is not None,
            platform=dataset.platform,
            original_url=dataset.original_url,
        )


def _assert_chat_available(state: _DatasetState | None) -> _DatasetState:
    """Return *state* if the dataset can answer questions, else raise ``409``.

    Chat is available exactly when the dataset exists, has an ``active_version``
    (its first processing finished successfully), and is not archived
    (Requirement 1.1). A refresh in flight or a failed refresh does **not** block
    chat — those keep the current ``active_version`` live — so only a missing
    active version or an archived dataset raises ``409 CHAT_UNAVAILABLE`` here
    (Requirements 1.2, 1.3). Returning the (non-``None``) state lets the caller
    use its fields without re-checking for ``None``.
    """
    if state is None or state.active_version is None:
        raise ConflictError(
            "This dataset isn't ready for questions yet.",
            code=CHAT_UNAVAILABLE_CODE,
        )
    if state.archived:
        raise ConflictError(
            "Restore this dataset to ask new questions.",
            code=CHAT_UNAVAILABLE_CODE,
        )
    return state


# ---------------------------------------------------------------------------
# Length validation + conversation handling
# ---------------------------------------------------------------------------


def _validate_question(raw: str) -> str:
    """Return the question to send, or raise ``422 INVALID_QUESTION``.

    Rejects an empty or whitespace-only question and one longer than
    :data:`MAX_QUESTION_CHARS` (design "Error Handling"; Requirement 6.1). The
    length is measured on the original text (what the analyst typed); emptiness
    is measured on the trimmed text so a question of only spaces is refused. The
    original (untrimmed) text is returned so leading/trailing whitespace the
    analyst included is preserved for the model — the Exchange records what was
    actually asked.
    """
    if not raw.strip():
        raise AppValidationError("Enter a question to ask.", code=INVALID_QUESTION_CODE)
    if len(raw) > MAX_QUESTION_CHARS:
        raise AppValidationError(
            f"Questions are limited to {MAX_QUESTION_CHARS} characters.",
            code=INVALID_QUESTION_CODE,
        )
    return raw


def _resolve_conversation_id(raw: str | None) -> str:
    """Return the conversation id to scope history by, minting one if absent.

    The browser tab normally supplies a random id (design: history is scoped to
    the asking tab's ``conversation_id``). When it is missing or blank — a fresh
    tab, or a client that didn't send one — a random id is minted so the Exchange
    is still saved and attributed to a conversation; it just has no prior turns
    to resolve against. The id identifies a conversation, not a person.
    """
    if raw and raw.strip():
        return raw.strip()
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Exchange assembly + persistence seam (Task 4.3)
# ---------------------------------------------------------------------------


def _build_exchange(
    *,
    dataset_id: str,
    data_version: int,
    conversation_id: str,
    asked_at: str,
    question: str,
    answer: str,
    citation_result: postprocess.CitationResult,
    scope_result: postprocess.ScopeResult,
    precheck_result: precheck.PrecheckResult | None,
    prompt_version: str,
    usage: dict[str, int],
) -> dict[str, Any]:
    """Assemble the Exchange payload sent in the ``done`` event (design "Data Models").

    Combines the post-processors' outputs into the single Exchange object the UI
    renders and Task 4.3 will persist: the cleaned ``answer``, the validated
    ``citations`` with their saved ``citation_snippets`` (so a citation popover
    keeps working after a refresh — Requirement 2.2), the ``scope`` /
    ``scope_category`` from scope tagging, the ``precheck`` summary, the model
    and prompt version, and token ``usage``. ``saved`` starts ``False``; the
    persistence seam flips it to ``True`` when the save succeeds (Task 4.3).

    The Exchange carries no visitor-identifying data (Requirement 5.1): the only
    identifier is the random ``conversation_id``, which is not a person.
    """
    model_id = get_settings().claude_chat_model
    precheck_summary: dict[str, Any] | None = None
    if precheck_result is not None:
        precheck_summary = {
            "label": precheck_result.label,
            "latency_ms": precheck_result.latency_ms,
        }
        if precheck_result.category:
            precheck_summary["category"] = precheck_result.category

    return {
        "id": str(uuid.uuid4()),
        "dataset_id": dataset_id,
        "data_version": data_version,
        "conversation_id": conversation_id,
        "asked_at": asked_at,
        "answered_at": _utc_now_iso(),
        "question": question,
        "answer": answer,
        "citations": citation_result.citations,
        "dropped_citations": citation_result.dropped_citations,
        "citation_snippets": citation_result.snippets_as_dict(),
        "scope": scope_result.scope,
        "scope_category": scope_result.scope_category,
        "precheck": precheck_summary,
        "model": model_id,
        "prompt_version": prompt_version,
        "usage": usage,
        "saved": False,
    }


def _persist_exchange(exchange: dict[str, Any]) -> dict[str, Any]:
    """Save the Exchange to S3, sign it, and publish the saved event (Task 4.3).

    Called once, just before the terminal ``done`` event, with the fully
    assembled Exchange content (``saved=False``). It:

    1. **Signs** the Exchange content with the server HMAC secret
       (``signing.sign_exchange``) and attaches the hex ``signature``. The
       signature is attached *first and unconditionally* so that even on a save
       failure the browser receives a signed payload it can replay to the
       retry-save endpoint, which verifies the same signature (Requirement 5.5;
       Task 5.2; design "Endpoints"). The ``saved`` flag is excluded from the
       signed content (see :mod:`app.chat.signing`), so flipping it below never
       invalidates the signature.
    2. **Writes** the Exchange JSON to ``datasets/{id}/chat/{iso_ts}-{uuid}.json``
       — the key comes from ``storage.keys.dataset_chat_exchange`` (steering:
       "every S3 key comes from storage.keys"); the ``iso_ts`` prefix makes S3's
       lexicographic listing chronological (design "Data Models"). The persisted
       bytes include the signature and ``saved=True``.
    3. On success, **publishes** ``chat.exchange.saved`` to EventBridge
       (``events.publisher.publish_event``) so the push consumer fans the new
       Exchange out to every browser (Requirement 5.7). The event body carries
       IDs only — ``dataset_id`` and the exchange ``id`` and S3 ``key`` — not the
       Exchange content (steering: "queue/event bodies carry IDs").
    4. On an S3 failure, logs it, leaves ``saved=False``, and does **not**
       publish the event. The signed payload (with ``saved=False``) is still
       returned so the UI keeps the answer shown and can offer a Retry
       (Requirement 5.5; design "Error Handling": "Save to S3 fails → SSE
       ``done`` with ``saved:false``").

    The Exchange's only identifier is the random ``conversation_id`` — no
    visitor-identifying data is written or published (Requirement 5.1). Returns
    the Exchange (mutated in place) to be sent in the ``done`` event.
    """
    settings = get_settings()
    exchange["signature"] = signing.sign_exchange(exchange, settings.chat_signing_secret)

    iso_ts = exchange["asked_at"]
    key = keys.dataset_chat_exchange(exchange["dataset_id"], iso_ts, exchange["id"])

    exchange["saved"] = True
    try:
        s3.put_bytes(
            key,
            json.dumps(exchange, default=str).encode("utf-8"),
            content_type=_EXCHANGE_CONTENT_TYPE,
        )
    except Exception as exc:  # noqa: BLE001 - any S3 error → saved:false, keep signature
        exchange["saved"] = False
        logger.warning(
            "chat_exchange_save_failed dataset_id=%s exchange_id=%s error=%s",
            exchange["dataset_id"],
            exchange["id"],
            exc,
            extra={
                "event": "chat_exchange_save_failed",
                "dataset_id": exchange["dataset_id"],
                "exchange_id": exchange["id"],
            },
        )
        return exchange

    publish_event(
        detail_type=_EXCHANGE_SAVED_EVENT,
        detail={
            "dataset_id": exchange["dataset_id"],
            "exchange_id": exchange["id"],
            "key": key,
        },
    )
    return exchange


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (Exchange timestamps)."""
    return datetime.now(UTC).isoformat()


def _enforce_question_rate_limit(client_ip: str | None, limit: int) -> None:
    """Apply the per-IP + global ``questions`` limit; carry ``Retry-After`` on 429.

    Re-raises :class:`RateLimitError` after parsing the retry delay from the
    limiter's message so the ``429`` carries a numeric ``Retry-After`` header
    (via :func:`_handle_rate_limited`). Mirrors ``ingestion.api``'s helper so
    the two services treat rate limits identically.
    """
    try:
        check_rate_limit(_QUESTIONS_ACTION, client_ip, limit)
    except RateLimitError as exc:
        setattr(exc, _RETRY_AFTER_ATTR, _retry_after_seconds(exc.message))
        raise


def _retry_after_seconds(message: str) -> int | None:
    """Pull the integer seconds out of the limiter's ``Retry after N seconds`` text."""
    for token in message.replace(".", " ").split():
        if token.isdigit():
            return int(token)
    return None
