"""Server-Sent Events (SSE) encoding for the streaming chat service.

The chat service streams its answer to the browser as a sequence of SSE events
(design "Architecture" / "Endpoints": the ``POST /api/chat/datasets/{id}``
response "Streams Server-Sent Events: ``token``, ``done``, ``error``"). This
module is the single, pure encoder for that wire format so the request flow
(Tasks 4.2/4.3) and the service skeleton (Task 4.1) all frame events the same
way and the format is unit-testable without a running server.

The wire format is the standard SSE framing
(https://html.spec.whatwg.org/multipage/server-sent-events.html): each event is

    event: <name>\\n
    data: <json>\\n
    \\n

with a trailing blank line terminating the event. The ``data`` payload is a
compact JSON object. A multi-line JSON value never occurs here (``json.dumps``
with no indent emits a single line), so one ``data:`` line per event is enough.

The same encoder runs unchanged in both compute modes: in Lambda mode the
AWS Lambda Web Adapter (``AWS_LWA_INVOKE_MODE=response_stream``) streams the
bytes back through the Function URL; in container mode uvicorn streams them
over HTTP. The application code never learns which mode it is in — it only
yields these encoded events (steering: "same code, both compute modes").
"""

from __future__ import annotations

import json
from typing import Any

#: Media type for an SSE response body. CloudFront passes ``text/event-stream``
#: through unbuffered when the origin streams and caching is disabled (see the
#: Edge stack's ``/api/chat/*`` behavior), so tokens reach the browser as they
#: are produced.
SSE_MEDIA_TYPE = "text/event-stream"

#: Response headers that keep the stream unbuffered end to end. ``no-cache``
#: stops any intermediary from buffering a complete response before forwarding,
#: and ``X-Accel-Buffering: no`` disables proxy buffering for the same reason.
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def encode_event(event: str, data: Any) -> str:  # noqa: ANN401 - any JSON-able payload
    """Return one SSE event frame for *event* carrying *data* as JSON.

    *event* is the SSE event name (``token``, ``done``, or ``error`` for the
    chat stream). *data* is serialized with a compact single-line JSON
    encoding, so the frame is always one ``event:`` line, one ``data:`` line,
    and the terminating blank line.
    """
    payload = json.dumps(data, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def token_event(text: str) -> str:
    """Return a ``token`` event carrying one streamed chunk of the answer."""
    return encode_event("token", {"text": text})


def done_event(payload: dict[str, Any]) -> str:
    """Return the terminal ``done`` event carrying the final payload.

    In the full flow (Task 4.3) *payload* is the saved Exchange (with a
    ``saved`` flag and an HMAC signature). Task 4.1 only needs the framing.
    """
    return encode_event("done", payload)


def error_event(code: str, message: str) -> str:
    """Return an ``error`` event in the shared error shape.

    Uses the same ``{"code", "message"}`` envelope the HTTP error handlers use
    (``core.errors``), with an ``UPPER_SNAKE_CASE`` code, so a mid-stream
    failure is reported to the browser consistently with a pre-stream HTTP
    error (design "Error Handling": "Claude error or timeout mid-stream → SSE
    ``error`` event").
    """
    return encode_event("error", {"code": code, "message": message})
