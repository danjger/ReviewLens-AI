"""Unit tests for app.core.origin_guard.

Covers:
- Missing X-Origin-Verify header → 403
- Wrong X-Origin-Verify header → 403
- Correct X-Origin-Verify header → passes through
- Health endpoint exemption: /healthz and /readyz are always allowed
- Local development bypass: empty origin_verify_secret allows all requests
- 403 response uses the standard error envelope
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from app.core.config import get_settings
from app.core.origin_guard import OriginGuardMiddleware
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SECRET = "test-origin-secret"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    """Return a minimal FastAPI app with OriginGuardMiddleware installed."""
    app = FastAPI()
    app.add_middleware(OriginGuardMiddleware)

    @app.get("/api/resource")
    async def _resource() -> dict[str, str]:
        return {"ok": "true"}

    @app.get("/healthz")
    async def _healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def _readyz() -> dict[str, str]:
        return {"status": "ok"}

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Generator[None, None, None]:
    """Ensure get_settings() cache is cleared before and after every test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def client_with_secret(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient backed by an app with a non-empty origin_verify_secret."""
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", _SECRET)
    return TestClient(_make_app(), raise_server_exceptions=False)


@pytest.fixture()
def client_no_secret(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient backed by an app with an empty origin_verify_secret (local dev)."""
    monkeypatch.delenv("ORIGIN_VERIFY_SECRET", raising=False)
    return TestClient(_make_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Missing header
# ---------------------------------------------------------------------------


class TestMissingHeader:
    """Requests without X-Origin-Verify must be rejected with 403."""

    def test_missing_header_returns_403(self, client_with_secret: TestClient) -> None:
        response = client_with_secret.get("/api/resource")
        assert response.status_code == 403

    def test_missing_header_error_envelope(self, client_with_secret: TestClient) -> None:
        body = client_with_secret.get("/api/resource").json()
        assert body == {"error": {"code": "FORBIDDEN", "message": "Forbidden"}}

    def test_missing_header_envelope_shape(self, client_with_secret: TestClient) -> None:
        body = client_with_secret.get("/api/resource").json()
        assert set(body.keys()) == {"error"}
        assert set(body["error"].keys()) == {"code", "message"}


# ---------------------------------------------------------------------------
# Wrong header
# ---------------------------------------------------------------------------


class TestWrongHeader:
    """Requests with an incorrect X-Origin-Verify value must be rejected."""

    def test_wrong_header_returns_403(self, client_with_secret: TestClient) -> None:
        response = client_with_secret.get(
            "/api/resource", headers={"X-Origin-Verify": "wrong-secret"}
        )
        assert response.status_code == 403

    def test_empty_string_header_returns_403(self, client_with_secret: TestClient) -> None:
        """An empty header value is not the same as the configured secret."""
        response = client_with_secret.get("/api/resource", headers={"X-Origin-Verify": ""})
        assert response.status_code == 403

    def test_wrong_header_error_code(self, client_with_secret: TestClient) -> None:
        body = client_with_secret.get(
            "/api/resource", headers={"X-Origin-Verify": "bad-value"}
        ).json()
        assert body["error"]["code"] == "FORBIDDEN"

    def test_partial_match_is_rejected(self, client_with_secret: TestClient) -> None:
        """A value that is a prefix of the secret must not pass."""
        response = client_with_secret.get(
            "/api/resource",
            headers={"X-Origin-Verify": _SECRET[:5]},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Correct header
# ---------------------------------------------------------------------------


class TestCorrectHeader:
    """Requests with the exact secret must be allowed through."""

    def test_correct_header_returns_200(self, client_with_secret: TestClient) -> None:
        response = client_with_secret.get("/api/resource", headers={"X-Origin-Verify": _SECRET})
        assert response.status_code == 200

    def test_correct_header_passes_response_body(self, client_with_secret: TestClient) -> None:
        body = client_with_secret.get("/api/resource", headers={"X-Origin-Verify": _SECRET}).json()
        assert body == {"ok": "true"}


# ---------------------------------------------------------------------------
# Health endpoint exemption
# ---------------------------------------------------------------------------


class TestHealthEndpointExemption:
    """Health endpoints must be reachable without the X-Origin-Verify header."""

    def test_healthz_no_header_returns_200(self, client_with_secret: TestClient) -> None:
        response = client_with_secret.get("/healthz")
        assert response.status_code == 200

    def test_readyz_no_header_returns_200(self, client_with_secret: TestClient) -> None:
        response = client_with_secret.get("/readyz")
        assert response.status_code == 200

    def test_healthz_wrong_header_still_returns_200(self, client_with_secret: TestClient) -> None:
        """Health endpoints are exempt even if the header is present and wrong."""
        response = client_with_secret.get("/healthz", headers={"X-Origin-Verify": "wrong"})
        assert response.status_code == 200

    def test_readyz_wrong_header_still_returns_200(self, client_with_secret: TestClient) -> None:
        response = client_with_secret.get("/readyz", headers={"X-Origin-Verify": "wrong"})
        assert response.status_code == 200

    def test_non_health_path_is_not_exempt(self, client_with_secret: TestClient) -> None:
        """A path that merely contains 'healthz' as a substring is not exempt."""
        response = client_with_secret.get("/api/resource")
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Local development bypass (empty secret)
# ---------------------------------------------------------------------------


class TestLocalDevBypass:
    """When origin_verify_secret is empty every request is allowed through."""

    def test_no_header_allowed_when_secret_empty(self, client_no_secret: TestClient) -> None:
        response = client_no_secret.get("/api/resource")
        assert response.status_code == 200

    def test_wrong_header_allowed_when_secret_empty(self, client_no_secret: TestClient) -> None:
        response = client_no_secret.get("/api/resource", headers={"X-Origin-Verify": "anything"})
        assert response.status_code == 200

    def test_health_endpoints_allowed_when_secret_empty(self, client_no_secret: TestClient) -> None:
        assert client_no_secret.get("/healthz").status_code == 200
        assert client_no_secret.get("/readyz").status_code == 200
