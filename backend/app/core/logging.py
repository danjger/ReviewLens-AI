"""Structured JSON logging for ReviewLens AI.

Every log record emitted through the standard ``logging`` module is
formatted as a single-line JSON object that includes these fields:

- ``timestamp``     ISO-8601 UTC of the log record
- ``level``         log level name (``INFO``, ``WARNING``, …)
- ``logger``        logger name (e.g. ``app.ingestion.normalizer``)
- ``message``       the formatted log message
- ``service``       ``Settings.service_name`` – set once at process startup
- ``instance_id``   UUID generated once at process startup; identifies the
                    Lambda invocation context or container instance
- ``correlation_id`` per-request UUID from ``_correlation_id`` ContextVar;
                     empty string when no request context is active
- ``dataset_id``    optional dataset UUID from ``_dataset_id`` ContextVar;
                     empty string when not set

Any extra keyword arguments passed to the log call (e.g.
``logger.info("…", extra={"page": 3})``) are merged into the record at
the top level.

Usage::

    from app.core.logging import configure_logging, set_correlation_id

    configure_logging()           # once at startup
    set_correlation_id("req-123")
    logging.getLogger(__name__).info("hello", extra={"page": 3})

The FastAPI middleware ``CorrelationIdMiddleware`` assigns a correlation ID
to every request automatically.

Note: this module name shadows the stdlib ``logging`` package for callers
inside ``app/``.  Use ``import logging`` for the standard library or
``from app.core import logging as app_logging`` to import this module.
"""

from __future__ import annotations

import json
import logging
import logging.config
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# ---------------------------------------------------------------------------
# Process-level identity (set once at import time)
# ---------------------------------------------------------------------------

#: A single UUID that identifies this process (Lambda context or container).
instance_id: str = str(uuid.uuid4())

#: The ``service_name`` from Settings; initialised by ``configure_logging``.
_service_name: str = "unknown"

# ---------------------------------------------------------------------------
# Request-scoped context variables
# ---------------------------------------------------------------------------

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
_dataset_id: ContextVar[str] = ContextVar("dataset_id", default="")


def set_correlation_id(cid: str) -> None:
    """Set the correlation ID for the current async context."""
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    """Return the correlation ID for the current async context."""
    return _correlation_id.get()


def set_dataset_id(did: str) -> None:
    """Set the dataset ID for the current async context."""
    _dataset_id.set(did)


def get_dataset_id() -> str:
    """Return the dataset ID for the current async context."""
    return _dataset_id.get()


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        # Let the base class populate record.message / record.exc_text etc.
        record.message = record.getMessage()
        if record.exc_info:
            # Render the traceback and attach it to the payload.
            record.exc_text = self.formatException(record.exc_info)

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
            "service": _service_name,
            "instance_id": instance_id,
            "correlation_id": _correlation_id.get(),
            "dataset_id": _dataset_id.get(),
        }

        if record.exc_text:
            payload["exc_info"] = record.exc_text

        # Merge any extra fields the caller passed via ``extra={…}``.
        _skip = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
            "message",
            "asctime",
        }
        for key, value in record.__dict__.items():
            if key not in _skip and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Public setup function
# ---------------------------------------------------------------------------


def configure_logging(service_name: str = "api", level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger.

    Call this once per process, typically in the FastAPI app factory or the
    consumer startup.  Subsequent calls from the same process are harmless
    (they replace the handlers, which is fine for tests that call it in
    fixtures).

    Args:
        service_name: Populates the ``service`` field in every log record.
        level: The minimum log level to emit.
    """
    global _service_name  # noqa: PLW0603
    _service_name = service_name

    formatter = _JsonFormatter()

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


# ---------------------------------------------------------------------------
# FastAPI middleware
# ---------------------------------------------------------------------------


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Assign a correlation ID to each HTTP request.

    If the incoming request carries an ``X-Correlation-Id`` header its value
    is reused; otherwise a fresh UUID4 is generated.  The ID is stored in
    ``_correlation_id`` (propagates automatically to async sub-tasks) and
    echoed back in the response as ``X-Correlation-Id``.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        cid = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
        set_correlation_id(cid)
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = cid
        return response
