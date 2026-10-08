"""Instrumented Anthropic Claude client.

This is the **only** place in the backend that talks to the Anthropic API.
Every AI call in ReviewLens AI MUST go through :class:`AiClient` so that:

- the global AI-call rate limit (``RL_GLOBAL_AI_CALLS_PER_HOUR``) is enforced
  *before* the model is invoked (Requirement 2.4);
- each call is logged with its purpose, model, input/output tokens, cache-hit
  tokens, and latency (Requirement 9.3);
- the client can be swapped for a test stub so unit and E2E tests never hit the
  network unless explicitly recording fixtures (Requirement 8.3).

Model IDs are always resolved from :class:`app.core.config.Settings` by a
*purpose* label — they are never hard-coded literals.

Usage::

    from app.core.ai import get_ai_client

    client = get_ai_client()
    message = client.create_message(
        purpose="precheck",
        messages=[{"role": "user", "content": "Is this a review page?"}],
        max_tokens=256,
    )

Purposes map to configured models:

======================  ========================================
purpose                 Settings field
======================  ========================================
``"chat"``              ``CLAUDE_CHAT_MODEL``
``"extract"`` /         ``CLAUDE_EXTRACT_MODEL``
``"extract_locator"`` /
``"profile"`` /
``"sentiment"`` /
``"themes"``
``"precheck"``          ``CLAUDE_PRECHECK_MODEL``
======================  ========================================

Testing
-------
``get_ai_client()`` returns a real :class:`AiClient` wrapping
``anthropic.Anthropic``.  Tests inject a stub by constructing
``AiClient(client=FakeClaude(...))`` or by calling
:func:`set_ai_client` with a pre-built instance; :func:`reset_ai_client`
restores the default.  The stub only needs to implement the small
``messages.create`` surface the real SDK exposes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

from app.core.config import get_settings
from app.core.errors import AppError, RateLimitError
from app.core.rate_limit import check_rate_limit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Purpose → config field resolution
# ---------------------------------------------------------------------------

#: Rate-limit action label shared by every AI call so one global counter caps
#: the total number of model invocations per window across all purposes.
AI_RATE_LIMIT_ACTION = "ai_calls"


def resolve_model(purpose: str) -> str:
    """Return the configured model ID for *purpose*.

    Model IDs always come from :class:`Settings`; this function is the single
    mapping from a purpose label to the right configured field.  Raises
    :class:`ValueError` for an unknown purpose so a typo fails loudly instead
    of silently picking the wrong model.
    """
    settings = get_settings()
    mapping = {
        "chat": settings.claude_chat_model,
        "extract": settings.claude_extract_model,
        "extract_locator": settings.claude_extract_model,
        "profile": settings.claude_extract_model,
        "sentiment": settings.claude_extract_model,
        "themes": settings.claude_extract_model,
        "precheck": settings.claude_precheck_model,
    }
    try:
        return mapping[purpose]
    except KeyError:
        raise ValueError(
            f"Unknown AI purpose {purpose!r}; expected one of {sorted(mapping)}"
        ) from None


# ---------------------------------------------------------------------------
# Minimal protocols for the subset of the Anthropic SDK we use
# ---------------------------------------------------------------------------


class _MessagesResource(Protocol):
    """The ``client.messages`` resource surface used by :class:`AiClient`."""

    def create(self, **kwargs: Any) -> Any:  # noqa: ANN401 - SDK passthrough
        ...

    def count_tokens(self, **kwargs: Any) -> Any:  # noqa: ANN401 - SDK passthrough
        ...


class _AnthropicLike(Protocol):
    """The subset of ``anthropic.Anthropic`` the instrumented client relies on."""

    @property
    def messages(self) -> _MessagesResource: ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AiRateLimitError(AppError):
    """Raised when the global AI-call limit is hit before invoking the model.

    Shares the ``RATE_LIMIT_EXCEEDED`` code / HTTP 429 of the generic rate-limit
    error so the HTTP layer maps it the same way, while being a distinct type
    callers can catch to tell "we throttled an AI call" apart from a per-IP
    request-rate rejection.
    """

    code = "RATE_LIMIT_EXCEEDED"
    status_code = 429


# ---------------------------------------------------------------------------
# Usage extraction helper
# ---------------------------------------------------------------------------


def _usage_fields(usage: Any) -> dict[str, int]:  # noqa: ANN401 - SDK usage object
    """Pull token counters off an Anthropic ``Usage`` object (or ``None``).

    Returns zeros for any field the object does not carry, so logging stays
    uniform regardless of whether prompt caching was involved.
    """

    def _as_int(value: Any) -> int:  # noqa: ANN401
        return int(value) if isinstance(value, int) else 0

    if usage is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    return {
        "input_tokens": _as_int(getattr(usage, "input_tokens", 0)),
        "output_tokens": _as_int(getattr(usage, "output_tokens", 0)),
        "cache_creation_input_tokens": _as_int(getattr(usage, "cache_creation_input_tokens", 0)),
        "cache_read_input_tokens": _as_int(getattr(usage, "cache_read_input_tokens", 0)),
    }


# ---------------------------------------------------------------------------
# Stream event helpers
# ---------------------------------------------------------------------------


def _stream_event_text(event: Any) -> str:  # noqa: ANN401 - SDK stream event
    """Return the text delta carried by a stream *event*, or ``""``.

    Claude's streamed text arrives on ``content_block_delta`` events whose
    ``delta`` is a ``text_delta`` carrying ``.text``. Every other event type
    (``message_start``, ``content_block_start``, ``ping``, ``message_stop``, …)
    carries no answer text, so this returns ``""`` for them. The access is
    defensive — an unknown or malformed event never raises — so a new event type
    from the provider is skipped rather than breaking the stream.
    """
    if getattr(event, "type", None) != "content_block_delta":
        return ""
    delta = getattr(event, "delta", None)
    text = getattr(delta, "text", None)
    return text if isinstance(text, str) else ""


def _accumulate_stream_usage(usage: StreamUsage, event: Any) -> None:  # noqa: ANN401 - SDK event
    """Fold any token counters on a stream *event* into *usage*.

    Input and cache-read counters appear on the opening ``message_start`` event
    (under ``event.message.usage``); the running ``output_tokens`` appears on the
    trailing ``message_delta`` events (under ``event.usage``). This reads
    whichever is present and keeps the latest non-zero value, so after the stream
    is exhausted *usage* reflects the final counts. All access is defensive so an
    event without usage is simply ignored.
    """
    message = getattr(event, "message", None)
    message_usage = getattr(message, "usage", None)
    if message_usage is not None:
        fields = _usage_fields(message_usage)
        if fields["input_tokens"]:
            usage.input_tokens = fields["input_tokens"]
        if fields["cache_creation_input_tokens"]:
            usage.cache_creation_input_tokens = fields["cache_creation_input_tokens"]
        if fields["cache_read_input_tokens"]:
            usage.cache_read_input_tokens = fields["cache_read_input_tokens"]

    event_usage = getattr(event, "usage", None)
    if event_usage is not None:
        fields = _usage_fields(event_usage)
        if fields["output_tokens"]:
            usage.output_tokens = fields["output_tokens"]
        # A streamed message_delta may also restate input/cache tokens.
        if fields["input_tokens"]:
            usage.input_tokens = fields["input_tokens"]
        if fields["cache_read_input_tokens"]:
            usage.cache_read_input_tokens = fields["cache_read_input_tokens"]


# ---------------------------------------------------------------------------
# Streaming usage accumulator
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StreamUsage:
    """Token usage collected while a streamed message is consumed.

    A streamed response (unlike :meth:`AiClient.create_message`) has no single
    final message object to read usage off; the counters arrive spread across
    the stream's events — ``input_tokens`` / cache tokens on the opening
    ``message_start`` event, ``output_tokens`` on the trailing ``message_delta``
    events. :meth:`AiClient.stream_message` fills this object **as the caller
    iterates**, so its fields are only final once the generator is exhausted.
    The chat endpoint reads it after the stream completes to populate the saved
    Exchange's ``usage`` block (design "Data Models").
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        """Return the usage as the Exchange's ``usage`` object (design keys)."""
        return {
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_input_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass(slots=True)
class StreamResult:
    """Handle to a streamed model call: a text generator plus live usage.

    :ivar text_chunks: A generator yielding each streamed text delta in order.
        The model call is only driven as this is consumed, so the endpoint can
        forward each chunk to the browser as an SSE ``token`` event the moment it
        arrives (Requirement 7.1). An error mid-stream propagates out of this
        generator so the endpoint can emit an SSE ``error`` event.
    :ivar usage: The :class:`StreamUsage` filled as the stream is consumed; read
        it only after the generator is exhausted.
    """

    text_chunks: Iterator[str]
    usage: StreamUsage = field(default_factory=StreamUsage)


# ---------------------------------------------------------------------------
# Instrumented client
# ---------------------------------------------------------------------------


class AiClient:
    """Wraps an Anthropic client with rate limiting and structured logging.

    Parameters
    ----------
    client:
        An object exposing ``messages.create(**kwargs)`` like
        ``anthropic.Anthropic``.  When ``None`` a real ``anthropic.Anthropic``
        is constructed; it reads the ``ANTHROPIC_API_KEY`` from the environment
        (populated from Secrets Manager by :mod:`app.core.config`), so the key
        never appears as a literal in code.
    """

    def __init__(self, client: _AnthropicLike | None = None) -> None:
        resolved: _AnthropicLike
        if client is None:
            # Imported lazily so tests that only use a stub don't require the
            # SDK's import side effects and so there is no module-level network
            # surface. The SDK reads ANTHROPIC_API_KEY from the environment.
            import anthropic  # noqa: PLC0415

            # The SDK's ``messages.create`` is heavily overloaded, so it does
            # not match the minimal Protocol structurally; cast to the surface
            # we actually use.
            resolved = cast("_AnthropicLike", anthropic.Anthropic())
        else:
            resolved = client
        self._client: _AnthropicLike = resolved

    def create_message(
        self,
        *,
        purpose: str,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 1024,
        **params: Any,  # noqa: ANN401 - passthrough to the SDK
    ) -> Any:  # noqa: ANN401 - returns the SDK Message
        """Invoke the model for *purpose*, enforcing limits and logging usage.

        The global AI-call rate limit is checked **before** the model is
        called; when it is exceeded :class:`AiRateLimitError` is raised and the
        underlying client is never invoked.  After a successful call, usage and
        latency are logged (Requirement 9.3) and the SDK response is returned
        unchanged.

        Parameters
        ----------
        purpose:
            Label for the call (e.g. ``"precheck"``, ``"extract_locator"``,
            ``"chat"``).  Selects the model when *model* is not given and tags
            the log line.
        messages:
            The Anthropic ``messages`` list.
        model:
            Override the model; defaults to the configured model for *purpose*.
        max_tokens:
            Maximum tokens to generate.
        **params:
            Extra keyword arguments forwarded verbatim to the SDK (``system``,
            ``temperature``, ``tools``, ...).
        """
        settings = get_settings()
        resolved_model = model if model is not None else resolve_model(purpose)

        # Enforce the GLOBAL AI-call limit before spending any money. A single
        # shared action means every purpose draws from one global counter.
        # client_ip=None → only the global counter is incremented/checked.
        # Re-raise as AiRateLimitError so callers can tell an AI throttle apart
        # from a per-IP request rejection; the HTTP status (429) is unchanged.
        try:
            check_rate_limit(
                AI_RATE_LIMIT_ACTION,
                None,
                settings.rl_global_ai_calls_per_hour,
            )
        except RateLimitError as exc:
            raise AiRateLimitError(
                "Global AI-call rate limit exceeded. " + exc.message,
            ) from exc

        start = time.monotonic()
        response = self._client.messages.create(
            model=resolved_model,
            messages=list(messages),
            max_tokens=max_tokens,
            **params,
        )
        latency_ms = round((time.monotonic() - start) * 1000, 2)

        usage = _usage_fields(getattr(response, "usage", None))
        logger.info(
            "ai_call purpose=%s model=%s latency_ms=%s",
            purpose,
            resolved_model,
            latency_ms,
            extra={
                "event": "ai_call",
                "purpose": purpose,
                "model": resolved_model,
                "latency_ms": latency_ms,
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
                "cache_read_input_tokens": usage["cache_read_input_tokens"],
            },
        )
        return response

    def stream_message(
        self,
        *,
        purpose: str,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 1024,
        **params: Any,  # noqa: ANN401 - passthrough to the SDK
    ) -> StreamResult:
        """Stream a model response for *purpose*, enforcing limits and logging usage.

        The streaming counterpart to :meth:`create_message`, used by the chat
        service so answers reach the browser token by token (Requirement 7.1).
        Like every other model call it goes through this one instrumented client,
        so the **global AI-call limit is checked before the model is invoked**
        and the call is logged once it finishes.

        The SDK's ``messages.create(stream=True)`` returns an iterable of raw
        stream events. This method returns a :class:`StreamResult` whose
        :attr:`~StreamResult.text_chunks` generator yields each text delta in
        order; the model is only driven as that generator is consumed, so the
        caller can forward chunks as they arrive. Token counters are accumulated
        onto :attr:`~StreamResult.usage` as the stream is read (they arrive
        across the ``message_start`` / ``message_delta`` events) and the summary
        ``ai_call`` log line is emitted when the stream is exhausted. An error
        raised by the provider mid-stream propagates out of the generator so the
        caller can surface it (the chat endpoint turns it into an SSE ``error``
        event; design "Error Handling").

        Parameters mirror :meth:`create_message`. Returns a :class:`StreamResult`.
        """
        settings = get_settings()
        resolved_model = model if model is not None else resolve_model(purpose)

        # Enforce the GLOBAL AI-call limit before spending any money — same gate
        # as create_message, applied before the stream is opened.
        try:
            check_rate_limit(
                AI_RATE_LIMIT_ACTION,
                None,
                settings.rl_global_ai_calls_per_hour,
            )
        except RateLimitError as exc:
            raise AiRateLimitError(
                "Global AI-call rate limit exceeded. " + exc.message,
            ) from exc

        usage = StreamUsage()

        def _generate() -> Iterator[str]:
            start = time.monotonic()
            stream = self._client.messages.create(
                model=resolved_model,
                messages=list(messages),
                max_tokens=max_tokens,
                stream=True,
                **params,
            )
            for event in stream:
                _accumulate_stream_usage(usage, event)
                text = _stream_event_text(event)
                if text:
                    yield text
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            logger.info(
                "ai_call purpose=%s model=%s latency_ms=%s streamed=1",
                purpose,
                resolved_model,
                latency_ms,
                extra={
                    "event": "ai_call",
                    "purpose": purpose,
                    "model": resolved_model,
                    "latency_ms": latency_ms,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                    "cache_read_input_tokens": usage.cache_read_input_tokens,
                },
            )

        return StreamResult(text_chunks=_generate(), usage=usage)

    def count_tokens(
        self,
        *,
        purpose: str,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        **params: Any,  # noqa: ANN401 - passthrough to the SDK
    ) -> int:
        """Count the input tokens *messages* would cost for *purpose*.

        Routes the Anthropic Token Count API (``messages.count_tokens``) through
        the single instrumented client so that, like every other AI call, the
        token counter can be swapped for a test stub and never hits the network
        in tests.  Token counting is a free, non-generative call, so it does
        **not** draw from the global generative-call limit; the call is still
        logged for visibility.

        The extraction cleaner uses this to size a Cleaned Page against the
        token budget before deciding whether to trim and chunk.

        Parameters
        ----------
        purpose:
            Label selecting the model when *model* is not given (e.g.
            ``"extract_locator"``) and tagging the log line.
        messages:
            The Anthropic ``messages`` list to measure.
        model:
            Override the model; defaults to the configured model for *purpose*.
        **params:
            Extra keyword arguments forwarded verbatim to the SDK (``tools``,
            ``system``, ...), so the count reflects what will actually be sent.

        Returns
        -------
        int
            The number of input tokens reported by the counter.
        """
        resolved_model = model if model is not None else resolve_model(purpose)

        start = time.monotonic()
        response = self._client.messages.count_tokens(
            model=resolved_model,
            messages=list(messages),
            **params,
        )
        latency_ms = round((time.monotonic() - start) * 1000, 2)

        input_tokens = getattr(response, "input_tokens", 0)
        input_tokens = int(input_tokens) if isinstance(input_tokens, int) else 0
        logger.info(
            "ai_count_tokens purpose=%s model=%s latency_ms=%s input_tokens=%s",
            purpose,
            resolved_model,
            latency_ms,
            input_tokens,
            extra={
                "event": "ai_count_tokens",
                "purpose": purpose,
                "model": resolved_model,
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
            },
        )
        return input_tokens


# ---------------------------------------------------------------------------
# Module-level factory (swappable in tests)
# ---------------------------------------------------------------------------

_ai_client: AiClient | None = None


def get_ai_client() -> AiClient:
    """Return the process-wide :class:`AiClient`, creating it on first use.

    Tests override the instance with :func:`set_ai_client` (passing an
    ``AiClient`` wrapping a ``FakeClaude``) so no network call is made.
    """
    global _ai_client  # noqa: PLW0603
    if _ai_client is None:
        _ai_client = AiClient()
    return _ai_client


def set_ai_client(client: AiClient) -> None:
    """Replace the process-wide client (used by tests to inject a stub)."""
    global _ai_client  # noqa: PLW0603
    _ai_client = client


def reset_ai_client() -> None:
    """Clear the cached client so the next :func:`get_ai_client` rebuilds it."""
    global _ai_client  # noqa: PLW0603
    _ai_client = None
