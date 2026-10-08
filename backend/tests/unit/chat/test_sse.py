"""Unit tests for the SSE encoder (``app.chat.sse``), guardrailed-chat Task 4.1.

The chat service streams its answer as Server-Sent Events (``token`` / ``done``
/ ``error``). These tests pin the wire format so the streaming endpoint and the
later request flow (Tasks 4.2/4.3) frame events the same way the browser's
``EventSource`` parses them: an ``event:`` line, a single-line ``data:`` JSON
line, and a terminating blank line.
"""

from __future__ import annotations

import json

from app.chat import sse


def _parse_frame(frame: str) -> tuple[str, dict]:
    """Return (event name, parsed data) from one SSE frame, asserting framing."""
    assert frame.endswith("\n\n"), "an SSE event must end with a blank line"
    lines = frame.rstrip("\n").split("\n")
    assert lines[0].startswith("event: ")
    assert lines[1].startswith("data: ")
    name = lines[0][len("event: ") :]
    data = json.loads(lines[1][len("data: ") :])
    return name, data


def test_token_event_frames_text() -> None:
    name, data = _parse_frame(sse.token_event("hello "))
    assert name == "token"
    assert data == {"text": "hello "}


def test_done_event_carries_payload() -> None:
    name, data = _parse_frame(sse.done_event({"dataset_id": "d1", "saved": False}))
    assert name == "done"
    assert data == {"dataset_id": "d1", "saved": False}


def test_error_event_uses_shared_envelope_shape() -> None:
    name, data = _parse_frame(sse.error_event("CHAT_FAILED", "boom"))
    assert name == "error"
    assert data == {"code": "CHAT_FAILED", "message": "boom"}


def test_data_payload_is_single_line() -> None:
    """A multi-line data payload would break SSE framing; JSON must be compact."""
    frame = sse.encode_event("token", {"text": "line1\nline2", "nested": {"a": 1}})
    data_lines = [ln for ln in frame.split("\n") if ln.startswith("data: ")]
    assert len(data_lines) == 1
    # The embedded newline is JSON-escaped, not a raw newline in the frame.
    assert "\\n" in data_lines[0]


def test_media_type_and_unbuffered_headers() -> None:
    assert sse.SSE_MEDIA_TYPE == "text/event-stream"
    # Buffering must be disabled so tokens stream incrementally end to end.
    assert sse.SSE_HEADERS["Cache-Control"] == "no-cache"
    assert sse.SSE_HEADERS["X-Accel-Buffering"] == "no"
