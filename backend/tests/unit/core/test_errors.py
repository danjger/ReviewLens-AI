"""Unit tests for app.core.errors.

Covers:
- AppError base class: code, message, and status_code attributes
- AppError constructor overrides for code and status_code
- Concrete subclasses: NotFoundError, ConflictError, AppValidationError, RateLimitError
- FastAPI handler for AppError returns the correct JSON envelope and status code
- FastAPI handler for RequestValidationError returns 422 with VALIDATION_ERROR code
- FastAPI catch-all handler for unhandled exceptions returns 500 with INTERNAL_ERROR
- Stack trace from unhandled exceptions appears in logs, NOT in the response body
"""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest
from app.core.errors import (
    AppError,
    AppValidationError,
    ConflictError,
    NotFoundError,
    RateLimitError,
    register_error_handlers,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app() -> tuple[FastAPI, TestClient]:
    """Return a minimal FastAPI app with all error handlers registered."""
    app = FastAPI()
    register_error_handlers(app)
    return app, TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# AppError base class
# ---------------------------------------------------------------------------


class TestAppErrorClass:
    """AppError must expose code, message, and status_code as instance attrs."""

    def test_default_code(self) -> None:
        err = AppError("something broke")
        assert err.code == "APP_ERROR"

    def test_default_status_code(self) -> None:
        err = AppError("something broke")
        assert err.status_code == 400

    def test_message_stored(self) -> None:
        err = AppError("my message")
        assert err.message == "my message"

    def test_is_exception(self) -> None:
        with pytest.raises(AppError):
            raise AppError("raised")

    def test_override_code_via_constructor(self) -> None:
        err = AppError("msg", code="CUSTOM_CODE")
        assert err.code == "CUSTOM_CODE"

    def test_override_status_code_via_constructor(self) -> None:
        err = AppError("msg", status_code=418)
        assert err.status_code == 418

    def test_override_both_via_constructor(self) -> None:
        err = AppError("msg", code="MY_ERR", status_code=418)
        assert err.code == "MY_ERR"
        assert err.status_code == 418

    def test_str_is_message(self) -> None:
        """Exception str() should be the message (standard Exception behaviour)."""
        err = AppError("the message")
        assert str(err) == "the message"


# ---------------------------------------------------------------------------
# Concrete subclasses
# ---------------------------------------------------------------------------


class TestSubclasses:
    """Each subclass must have the correct code and status_code."""

    def test_not_found_error(self) -> None:
        err = NotFoundError("dataset missing")
        assert err.code == "NOT_FOUND"
        assert err.status_code == 404
        assert err.message == "dataset missing"

    def test_conflict_error(self) -> None:
        err = ConflictError("already exists")
        assert err.code == "CONFLICT"
        assert err.status_code == 409

    def test_app_validation_error(self) -> None:
        err = AppValidationError("invalid field")
        assert err.code == "VALIDATION_ERROR"
        assert err.status_code == 422

    def test_rate_limit_error(self) -> None:
        err = RateLimitError("slow down")
        assert err.code == "RATE_LIMIT_EXCEEDED"
        assert err.status_code == 429

    def test_subclasses_are_app_errors(self) -> None:
        for cls in (NotFoundError, ConflictError, AppValidationError, RateLimitError):
            assert issubclass(cls, AppError)


# ---------------------------------------------------------------------------
# AppError handler via FastAPI
# ---------------------------------------------------------------------------


class TestAppErrorHandler:
    """register_error_handlers() must translate AppError into the JSON envelope."""

    def test_not_found_returns_404(self) -> None:
        app, client = _make_app()

        @app.get("/resource")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise NotFoundError("Widget not found")

        response = client.get("/resource")
        assert response.status_code == 404

    def test_not_found_envelope(self) -> None:
        app, client = _make_app()

        @app.get("/resource")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise NotFoundError("Widget not found")

        body = client.get("/resource").json()
        assert body == {"error": {"code": "NOT_FOUND", "message": "Widget not found"}}

    def test_conflict_returns_409(self) -> None:
        app, client = _make_app()

        @app.get("/dup")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise ConflictError("URL already tracked")

        response = client.get("/dup")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFLICT"

    def test_app_validation_returns_422(self) -> None:
        app, client = _make_app()

        @app.get("/validate")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise AppValidationError("bad value")

        response = client.get("/validate")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_rate_limit_returns_429(self) -> None:
        app, client = _make_app()

        @app.get("/limited")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise RateLimitError("too many requests")

        response = client.get("/limited")
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"

    def test_custom_app_error_code_and_status(self) -> None:
        app, client = _make_app()

        @app.get("/custom")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise AppError("custom problem", code="CUSTOM_CODE", status_code=418)

        response = client.get("/custom")
        assert response.status_code == 418
        assert response.json()["error"]["code"] == "CUSTOM_CODE"

    def test_envelope_has_error_key_only(self) -> None:
        """The response body must be exactly {"error": {...}} — no extra keys."""
        app, client = _make_app()

        @app.get("/strict")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise NotFoundError("gone")

        body = client.get("/strict").json()
        assert set(body.keys()) == {"error"}
        assert set(body["error"].keys()) == {"code", "message"}

    def test_content_type_is_json(self) -> None:
        app, client = _make_app()

        @app.get("/ct")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise NotFoundError("ct test")

        response = client.get("/ct")
        assert "application/json" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# RequestValidationError handler
# ---------------------------------------------------------------------------


class TestRequestValidationErrorHandler:
    """FastAPI / Pydantic validation errors must become 422 VALIDATION_ERROR."""

    def test_missing_required_query_param_returns_422(self) -> None:
        app, client = _make_app()

        @app.get("/search")
        async def _route(q: str) -> dict:  # type: ignore[type-arg]
            return {"q": q}

        response = client.get("/search")  # missing required `q`
        assert response.status_code == 422

    def test_validation_error_code(self) -> None:
        app, client = _make_app()

        @app.get("/typed")
        async def _route(count: int) -> dict:  # type: ignore[type-arg]
            return {"count": count}

        body = client.get("/typed?count=not-an-int").json()
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_validation_error_has_message(self) -> None:
        app, client = _make_app()

        @app.get("/msg")
        async def _route(n: int) -> dict:  # type: ignore[type-arg]
            return {"n": n}

        body = client.get("/msg?n=bad").json()
        assert isinstance(body["error"]["message"], str)
        assert len(body["error"]["message"]) > 0

    def test_validation_envelope_shape(self) -> None:
        app, client = _make_app()

        @app.get("/shape")
        async def _route(x: int) -> dict:  # type: ignore[type-arg]
            return {"x": x}

        body = client.get("/shape?x=bad").json()
        assert set(body.keys()) == {"error"}
        assert set(body["error"].keys()) == {"code", "message"}


# ---------------------------------------------------------------------------
# Catch-all exception handler
# ---------------------------------------------------------------------------


class TestCatchAllHandler:
    """Unhandled exceptions must return 500 with INTERNAL_ERROR; trace goes to logs."""

    def test_unhandled_exception_returns_500(self) -> None:
        app, client = _make_app()

        @app.get("/crash")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise RuntimeError("something went very wrong")

        response = client.get("/crash")
        assert response.status_code == 500

    def test_unhandled_exception_code(self) -> None:
        app, client = _make_app()

        @app.get("/boom")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise ValueError("internal state corrupt")

        body = client.get("/boom").json()
        assert body["error"]["code"] == "INTERNAL_ERROR"

    def test_unhandled_exception_generic_message(self) -> None:
        """The response must not reveal internal exception details."""
        app, client = _make_app()

        @app.get("/secret-error")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise RuntimeError("DB password is hunter2")

        body = client.get("/secret-error").json()
        assert body["error"]["message"] == "An unexpected error occurred"
        # The real error text must NOT leak into the response
        assert "hunter2" not in json.dumps(body)

    def test_stack_trace_not_in_response(self) -> None:
        app, client = _make_app()

        @app.get("/trace")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise RuntimeError("internal detail")

        body = client.get("/trace").json()
        response_text = json.dumps(body)
        assert "Traceback" not in response_text
        assert "RuntimeError" not in response_text

    def test_stack_trace_written_to_log(self) -> None:
        """The full traceback must appear in the log output."""
        app, client = _make_app()

        @app.get("/logged")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise RuntimeError("log this traceback please")

        # Capture log output from the errors module logger.
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.ERROR)
        errors_logger = logging.getLogger("app.core.errors")
        errors_logger.addHandler(handler)
        original_level = errors_logger.level
        errors_logger.setLevel(logging.ERROR)

        try:
            client.get("/logged")
        finally:
            errors_logger.removeHandler(handler)
            errors_logger.setLevel(original_level)

        log_output = stream.getvalue()
        assert "RuntimeError" in log_output or "log this traceback please" in log_output

    def test_envelope_shape_on_500(self) -> None:
        app, client = _make_app()

        @app.get("/500-shape")
        async def _route() -> dict:  # type: ignore[type-arg]
            raise Exception("generic")  # noqa: TRY002

        body = client.get("/500-shape").json()
        assert set(body.keys()) == {"error"}
        assert set(body["error"].keys()) == {"code", "message"}

    def test_zero_division_returns_500(self) -> None:
        app, client = _make_app()

        @app.get("/div")
        async def _route() -> dict:  # type: ignore[type-arg]
            _ = 1 / 0
            return {}

        assert client.get("/div").status_code == 500
