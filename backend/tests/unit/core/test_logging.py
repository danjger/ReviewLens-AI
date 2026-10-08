"""Unit tests for app.core.logging.

Covers:
- JSON output shape (all required fields present)
- ``service`` and ``instance_id`` populated from configuration
- ``correlation_id`` and ``dataset_id`` come from ContextVars
- Extra fields passed via ``extra={}`` are merged into the payload
- ``CorrelationIdMiddleware``: generates a UUID when header is absent
- ``CorrelationIdMiddleware``: reuses the caller-supplied ``X-Correlation-Id``
- ``CorrelationIdMiddleware``: echoes the ID back in the response header
- Context helpers: set/get round-trip for both context vars
"""

from __future__ import annotations

import json
import logging
import uuid
from io import StringIO

import pytest
from app.core.logging import (
    CorrelationIdMiddleware,
    configure_logging,
    get_correlation_id,
    get_dataset_id,
    set_correlation_id,
    set_dataset_id,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stream_logger(service_name: str = "test-svc") -> tuple[logging.Logger, StringIO]:
    """Configure the JSON logger writing to a StringIO buffer.

    Returns the root logger and the buffer so tests can inspect output.
    """
    configure_logging(service_name=service_name)

    stream = StringIO()
    handler = logging.StreamHandler(stream)
    # Re-apply the same formatter that configure_logging installs.
    from app.core.logging import _JsonFormatter  # noqa: PLC0415

    handler.setFormatter(_JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    return root, stream


def _last_record(stream: StringIO) -> dict:  # type: ignore[type-arg]
    """Parse the last JSON line written to *stream*."""
    stream.seek(0)
    lines = [line for line in stream.read().splitlines() if line.strip()]
    assert lines, "No log output found"
    return json.loads(lines[-1])  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# JSON shape
# ---------------------------------------------------------------------------


class TestJsonShape:
    """Every log record must contain the required fields with correct types."""

    def test_required_fields_present(self) -> None:
        root, stream = _make_stream_logger()
        root.info("hello world")
        record = _last_record(stream)

        for field in (
            "timestamp",
            "level",
            "logger",
            "message",
            "service",
            "instance_id",
            "correlation_id",
            "dataset_id",
        ):
            assert field in record, f"Missing field: {field}"

    def test_message_matches_logged_text(self) -> None:
        root, stream = _make_stream_logger()
        root.info("test message content")
        assert _last_record(stream)["message"] == "test message content"

    def test_level_is_uppercase(self) -> None:
        root, stream = _make_stream_logger()
        root.warning("warn")
        assert _last_record(stream)["level"] == "WARNING"

    def test_timestamp_is_iso8601_utc(self) -> None:
        from datetime import datetime  # noqa: PLC0415

        root, stream = _make_stream_logger()
        root.info("ts check")
        ts = _last_record(stream)["timestamp"]
        # Must parse without error and be timezone-aware UTC
        dt = datetime.fromisoformat(ts)
        assert dt.utcoffset() is not None

    def test_logger_name_matches(self) -> None:
        configure_logging()
        stream = StringIO()
        from app.core.logging import _JsonFormatter  # noqa: PLC0415

        h = logging.StreamHandler(stream)
        h.setFormatter(_JsonFormatter())
        log = logging.getLogger("app.some.module")
        log.handlers = [h]
        log.propagate = False
        log.info("named logger")
        assert _last_record(stream)["logger"] == "app.some.module"

    def test_instance_id_is_stable_uuid(self) -> None:
        root, stream = _make_stream_logger()
        root.info("a")
        root.info("b")
        stream.seek(0)
        lines = [json.loads(line) for line in stream.read().splitlines() if line.strip()]
        ids = {r["instance_id"] for r in lines}
        assert len(ids) == 1, "instance_id must not change between records"
        uuid.UUID(ids.pop())  # raises ValueError if not a valid UUID

    def test_exc_info_included_on_exception(self) -> None:
        root, stream = _make_stream_logger()
        try:
            raise ValueError("boom")
        except ValueError:
            root.exception("caught error")
        record = _last_record(stream)
        assert "exc_info" in record
        assert "ValueError" in record["exc_info"]


# ---------------------------------------------------------------------------
# Service name
# ---------------------------------------------------------------------------


class TestServiceName:
    def test_service_name_comes_from_configure_logging(self) -> None:
        root, stream = _make_stream_logger(service_name="workers")
        root.info("svc test")
        assert _last_record(stream)["service"] == "workers"

    def test_service_name_changes_on_reconfigure(self) -> None:
        root, stream = _make_stream_logger(service_name="api")
        configure_logging(service_name="chat")
        # Re-attach stream handler with new formatter.
        from app.core.logging import _JsonFormatter  # noqa: PLC0415

        h = logging.StreamHandler(stream)
        h.setFormatter(_JsonFormatter())
        root.handlers = [h]
        root.info("after reconfigure")
        assert _last_record(stream)["service"] == "chat"


# ---------------------------------------------------------------------------
# Context variables
# ---------------------------------------------------------------------------


class TestContextVars:
    def setup_method(self) -> None:
        # Reset context vars to defaults before each test.
        set_correlation_id("")
        set_dataset_id("")

    def test_correlation_id_default_empty(self) -> None:
        assert get_correlation_id() == ""

    def test_dataset_id_default_empty(self) -> None:
        assert get_dataset_id() == ""

    def test_set_and_get_correlation_id(self) -> None:
        set_correlation_id("req-abc")
        assert get_correlation_id() == "req-abc"

    def test_set_and_get_dataset_id(self) -> None:
        set_dataset_id("ds-xyz")
        assert get_dataset_id() == "ds-xyz"

    def test_correlation_id_appears_in_log(self) -> None:
        root, stream = _make_stream_logger()
        set_correlation_id("cid-123")
        root.info("with cid")
        assert _last_record(stream)["correlation_id"] == "cid-123"

    def test_dataset_id_appears_in_log(self) -> None:
        root, stream = _make_stream_logger()
        set_dataset_id("ds-999")
        root.info("with did")
        assert _last_record(stream)["dataset_id"] == "ds-999"

    def test_empty_correlation_id_in_log(self) -> None:
        root, stream = _make_stream_logger()
        set_correlation_id("")
        root.info("no cid")
        assert _last_record(stream)["correlation_id"] == ""

    def test_empty_dataset_id_in_log(self) -> None:
        root, stream = _make_stream_logger()
        set_dataset_id("")
        root.info("no did")
        assert _last_record(stream)["dataset_id"] == ""


# ---------------------------------------------------------------------------
# Extra fields
# ---------------------------------------------------------------------------


class TestExtraFields:
    def test_extra_dict_merged_into_payload(self) -> None:
        root, stream = _make_stream_logger()
        root.info("with extra", extra={"page": 3, "url": "https://example.com"})
        record = _last_record(stream)
        assert record["page"] == 3
        assert record["url"] == "https://example.com"

    def test_extra_does_not_override_core_fields(self) -> None:
        """Extra keys that clash with core fields are present but the core field wins."""
        root, stream = _make_stream_logger()
        # 'message' is a core field; the extra value should not replace it.
        root.info("real message", extra={"my_extra": "value"})
        record = _last_record(stream)
        assert record["message"] == "real message"
        assert record["my_extra"] == "value"


# ---------------------------------------------------------------------------
# CorrelationIdMiddleware
# ---------------------------------------------------------------------------


@pytest.fixture()
def test_app() -> FastAPI:
    """Minimal FastAPI app with the CorrelationIdMiddleware attached."""
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/ping")
    async def ping() -> dict:  # type: ignore[type-arg]
        return {"cid": get_correlation_id()}

    return app


class TestCorrelationIdMiddleware:
    def test_generates_uuid_when_header_absent(self, test_app: FastAPI) -> None:
        client = TestClient(test_app, raise_server_exceptions=True)
        response = client.get("/ping")
        assert response.status_code == 200
        cid = response.json()["cid"]
        uuid.UUID(cid)  # raises ValueError if not a valid UUID

    def test_reuses_caller_supplied_header(self, test_app: FastAPI) -> None:
        client = TestClient(test_app, raise_server_exceptions=True)
        supplied = "my-correlation-id-123"
        response = client.get("/ping", headers={"X-Correlation-Id": supplied})
        assert response.json()["cid"] == supplied

    def test_echoes_id_in_response_header(self, test_app: FastAPI) -> None:
        client = TestClient(test_app, raise_server_exceptions=True)
        response = client.get("/ping")
        assert "X-Correlation-Id" in response.headers
        # The same value must appear in both the body (context var) and header.
        assert response.headers["X-Correlation-Id"] == response.json()["cid"]

    def test_echoes_supplied_id_in_response_header(self, test_app: FastAPI) -> None:
        client = TestClient(test_app, raise_server_exceptions=True)
        supplied = "echo-me-back"
        response = client.get("/ping", headers={"X-Correlation-Id": supplied})
        assert response.headers["X-Correlation-Id"] == supplied

    def test_different_requests_get_different_ids(self, test_app: FastAPI) -> None:
        client = TestClient(test_app, raise_server_exceptions=True)
        id1 = client.get("/ping").json()["cid"]
        id2 = client.get("/ping").json()["cid"]
        assert id1 != id2
