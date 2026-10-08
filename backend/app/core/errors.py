"""Application error classes and FastAPI error handler registration.

All application errors extend ``AppError``, which carries a machine-readable
``code`` (``UPPER_SNAKE_CASE``), a human-readable ``message``, and an HTTP
``status_code``.  The ``register_error_handlers`` function installs three
handlers on a FastAPI application:

1. ``AppError`` → structured JSON envelope with the error's own status code.
2. ``RequestValidationError`` (Pydantic / FastAPI input validation) → 422 with
   code ``VALIDATION_ERROR``.
3. ``Exception`` (catch-all) → 500 with code ``INTERNAL_ERROR`` and a generic
   message.  The stack trace is written to the log; it is **never** included in
   the response body.

All handlers return the same JSON envelope::

    {"error": {"code": "UPPER_SNAKE_CASE", "message": "Human-readable text"}}

Usage::

    from fastapi import FastAPI
    from app.core.errors import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error envelope helper
# ---------------------------------------------------------------------------


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    """Return the standard ``{"error": {"code": ..., "message": ...}}`` envelope."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class AppError(Exception):
    """Base class for all application errors.

    Subclass this to create domain-specific errors.  Override ``code`` and
    ``status_code`` as class attributes, or pass them to the constructor.

    Example::

        raise NotFoundError("Dataset not found")
        raise AppError("Custom problem", code="MY_ERROR", status_code=400)
    """

    code: str = "APP_ERROR"
    status_code: int = 400

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


# ---------------------------------------------------------------------------
# Concrete subclasses
# ---------------------------------------------------------------------------


class NotFoundError(AppError):
    """Raised when a requested resource does not exist (HTTP 404)."""

    code = "NOT_FOUND"
    status_code = 404


class ConflictError(AppError):
    """Raised when a request conflicts with existing state (HTTP 409)."""

    code = "CONFLICT"
    status_code = 409


class AppValidationError(AppError):
    """Raised when application-level validation fails (HTTP 422).

    Named ``AppValidationError`` to avoid clashing with
    ``fastapi.exceptions.RequestValidationError`` and Pydantic's own
    ``ValidationError``.
    """

    code = "VALIDATION_ERROR"
    status_code = 422


class RateLimitError(AppError):
    """Raised when a rate limit is exceeded (HTTP 429)."""

    code = "RATE_LIMIT_EXCEEDED"
    status_code = 429


class RetryableError(AppError):
    """Base class for transient failures that a caller may safely retry.

    Queue handlers let these propagate so the message is redelivered (and
    eventually sent to the DLQ) rather than being treated as a permanent
    failure.  Subclass this for a specific transient condition (for example a
    temporarily unavailable AI provider).  Maps to HTTP 503 when surfaced
    through the API.
    """

    code = "RETRYABLE_ERROR"
    status_code = 503


# ---------------------------------------------------------------------------
# FastAPI error handler registration
# ---------------------------------------------------------------------------


def register_error_handlers(app: FastAPI) -> None:
    """Register the three application-wide error handlers on *app*.

    Call this once after creating the ``FastAPI`` instance and before the app
    starts handling requests.
    """

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """Return the structured envelope for any ``AppError``."""
        return _error_response(exc.code, exc.message, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Translate FastAPI / Pydantic request-validation errors to the envelope."""
        # Build a concise human-readable summary from the Pydantic error list.
        errors: list[Any] = list(exc.errors())
        if errors:
            first = errors[0]
            loc = " → ".join(str(p) for p in first.get("loc", []))
            detail = first.get("msg", "invalid input")
            message = f"{loc}: {detail}" if loc else detail
        else:
            message = "Invalid request"
        return _error_response("VALIDATION_ERROR", message, 422)

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Log the full traceback and return a generic 500 response."""
        logger.exception(
            "Unhandled exception on %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        return _error_response(
            "INTERNAL_ERROR",
            "An unexpected error occurred",
            500,
        )
