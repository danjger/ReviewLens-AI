"""Test stub for the Anthropic client: ``FakeClaude``.

``FakeClaude`` mimics the small ``messages.create(**kwargs)`` surface that
:class:`app.core.ai.AiClient` uses, replaying responses recorded under
``tests/fixtures/ai/`` instead of calling the network.  Tests wire it in with::

    from app.core.ai import AiClient, set_ai_client
    from tests.support.ai import FakeClaude

    set_ai_client(AiClient(client=FakeClaude()))

Fixtures are plain JSON files named ``{key}.json`` where *key* is a stable hash
of the request (purpose + model + messages + params).  A missing fixture raises
:class:`MissingFixtureError`, whose message tells the developer to run
``make record-ai`` to record it against the live model.

Record / replay
---------------
When the ``RECORD_AI`` environment variable is set (as ``make record-ai`` does)
and an ``ANTHROPIC_API_KEY`` is available, construct the stub with a live
delegate so missing fixtures are fetched from the real model and written to
disk for future replays::

    import anthropic
    from tests.support.ai import FakeClaude

    stub = FakeClaude(record_delegate=anthropic.Anthropic())  # only under RECORD_AI

The replay path (``record_delegate=None``) never touches the network, so the
default test run is fully offline and deterministic.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Directory holding recorded AI responses. Resolved relative to this file so it
# works regardless of the pytest invocation directory.
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "ai"


class MissingFixtureError(RuntimeError):
    """Raised when no recorded fixture exists for a request and we can't record."""


def _canonical_request(
    *, model: str, messages: list[dict[str, Any]], params: dict[str, Any]
) -> str:
    """Return a stable JSON string for the request used to key fixtures."""
    payload = {"model": model, "messages": messages, "params": params}
    return json.dumps(payload, sort_keys=True, default=str)


def request_key(*, model: str, messages: list[dict[str, Any]], params: dict[str, Any]) -> str:
    """Return the fixture key (sha256 hex) for a request."""
    canonical = _canonical_request(model=model, messages=messages, params=params)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _to_namespace(obj: Any) -> Any:  # noqa: ANN401 - recursive JSON → attrs
    """Recursively turn dicts/lists into attribute-accessible namespaces.

    This lets replayed responses expose ``response.usage.input_tokens`` and
    ``response.content[0].text`` just like the real SDK's pydantic models,
    which the instrumented client reads via ``getattr``.
    """
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_to_namespace(v) for v in obj]
    return obj


def _response_to_dict(response: Any) -> dict[str, Any]:  # noqa: ANN401 - SDK model
    """Serialise an SDK response (or any object) to a plain dict for recording."""
    if hasattr(response, "model_dump"):
        dumped: dict[str, Any] = response.model_dump(mode="json")
        return dumped
    if isinstance(response, dict):
        return response
    raise TypeError(f"Cannot serialise response of type {type(response)!r} for recording")


def _message_text(data: dict[str, Any]) -> str:
    """Concatenate the text of a recorded (non-streaming) message's content."""
    parts: list[str] = []
    for block in data.get("content", []) or []:
        if isinstance(block, dict) and block.get("type", "text") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _synthesize_stream_events(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a recorded non-streaming message into a plausible event sequence.

    Lets a test (or a recorded non-streaming fixture) drive the streaming code
    path without a separately recorded event list: the message text is split
    into word chunks emitted as ``content_block_delta`` events, bracketed by a
    ``message_start`` (carrying input/cache usage) and a ``message_delta``
    (carrying output usage), mirroring the real stream's shape.
    """
    text = _message_text(data)
    usage = data.get("usage", {}) or {}
    words = text.split(" ")
    deltas: list[dict[str, Any]] = []
    for index, word in enumerate(words):
        chunk = word if index == len(words) - 1 else f"{word} "
        if not chunk:
            continue
        deltas.append(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}}
        )
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": usage.get("input_tokens", 0),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                }
            },
        },
        *deltas,
        {"type": "message_delta", "usage": {"output_tokens": usage.get("output_tokens", 0)}},
    ]
    return events


def _replay_stream(data: Any) -> Any:  # noqa: ANN401 - fixture shape varies
    """Yield replayed stream events from a recorded fixture.

    Accepts either a list of raw events (a recorded live stream) or a single
    recorded message dict, which is synthesized into events by
    :func:`_synthesize_stream_events`. Each event is returned as an
    attribute-accessible namespace so the instrumented client reads it exactly
    like the real SDK's typed events.
    """
    events = data if isinstance(data, list) else _synthesize_stream_events(data)
    for event in events:
        yield _to_namespace(event)


class _Messages:
    """Implements the ``messages`` resource with a ``create`` method."""

    def __init__(self, parent: FakeClaude) -> None:
        self._parent = parent

    def create(self, **kwargs: Any) -> Any:  # noqa: ANN401 - mirrors SDK signature
        model = kwargs.get("model", "")
        messages = kwargs.get("messages", [])
        streaming = bool(kwargs.get("stream", False))
        params = {k: v for k, v in kwargs.items() if k not in ("model", "messages")}
        key = request_key(model=model, messages=messages, params=params)
        fixture_path = self._parent.fixtures_dir / f"{key}.json"

        if fixture_path.exists():
            data = json.loads(fixture_path.read_text())
            if streaming:
                return _replay_stream(data)
            return _to_namespace(data)

        # No fixture. In record mode, call the live model and save the result.
        if self._parent.record_delegate is not None:
            live_response = self._parent.record_delegate.messages.create(**kwargs)
            self._parent.fixtures_dir.mkdir(parents=True, exist_ok=True)
            if streaming:
                # The live stream is an iterable of events; record them as a
                # list so a later replay reproduces the same event sequence.
                events = [_response_to_dict(event) for event in live_response]
                fixture_path.write_text(json.dumps(events, indent=2))
                return _replay_stream(events)
            fixture_path.write_text(json.dumps(_response_to_dict(live_response), indent=2))
            data = json.loads(fixture_path.read_text())
            return _to_namespace(data)

        raise MissingFixtureError(
            f"No recorded AI fixture at {fixture_path}. "
            f"Run `make record-ai` (needs ANTHROPIC_API_KEY) to record it."
        )

    def count_tokens(self, **kwargs: Any) -> Any:  # noqa: ANN401 - mirrors SDK signature
        """Estimate input tokens offline, mirroring ``messages.count_tokens``.

        The real Token Count API is free and non-generative, so there is no
        value in recording fixtures for it: tests only need a deterministic,
        monotonic estimate so the cleaner's budget logic is exercised the same
        way every run.  We approximate the common ``~4 characters per token``
        heuristic over the concatenated message text and return a namespace with
        an ``input_tokens`` attribute, matching the SDK's ``MessageTokensCount``
        surface the instrumented client reads.
        """
        messages = kwargs.get("messages", [])
        char_count = 0
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, str):
                char_count += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        char_count += len(str(block.get("text", "")))
                    else:
                        char_count += len(str(block))
            else:
                char_count += len(str(content))
        input_tokens = (char_count + 3) // 4
        return SimpleNamespace(input_tokens=input_tokens)


class FakeClaude:
    """Offline replacement for ``anthropic.Anthropic`` used in tests.

    Parameters
    ----------
    fixtures_dir:
        Directory to read/write fixtures from. Defaults to ``tests/fixtures/ai``.
    record_delegate:
        When provided (only under ``RECORD_AI``), a real Anthropic-like client
        used to fetch and record responses for requests that have no fixture.
        When ``None`` (the default), the stub is fully offline and a missing
        fixture raises :class:`MissingFixtureError`.
    """

    def __init__(
        self,
        *,
        fixtures_dir: Path | None = None,
        record_delegate: Any | None = None,  # noqa: ANN401 - Anthropic-like
    ) -> None:
        self.fixtures_dir = fixtures_dir if fixtures_dir is not None else FIXTURES_DIR
        self.record_delegate = record_delegate
        self.messages = _Messages(self)

    @classmethod
    def from_env(cls, *, fixtures_dir: Path | None = None) -> FakeClaude:
        """Build a stub, enabling record mode when ``RECORD_AI`` is set.

        Under ``RECORD_AI=1`` with an ``ANTHROPIC_API_KEY`` present, a live
        Anthropic client is used as the record delegate so ``make record-ai``
        can capture new fixtures. Otherwise the stub is offline (replay only).
        """
        delegate: Any | None = None
        if os.environ.get("RECORD_AI") and os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic  # noqa: PLC0415

            delegate = anthropic.Anthropic()
        return cls(fixtures_dir=fixtures_dir, record_delegate=delegate)
