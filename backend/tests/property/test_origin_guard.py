"""Property-based tests for app.core.origin_guard.

Property 3: Origin guard.
  For any request whose X-Origin-Verify header is missing or differs from
  the secret, every non-health endpoint SHALL refuse it.
  Validates: Requirement 2.3
"""

from __future__ import annotations

import os
from unittest.mock import patch

from app.core.config import get_settings
from app.core.origin_guard import OriginGuardMiddleware
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_client() -> TestClient:
    """Return a TestClient for a minimal app with OriginGuardMiddleware."""
    app = FastAPI()
    app.add_middleware(OriginGuardMiddleware)

    @app.get("/api/data")
    async def _data() -> dict[str, str]:
        return {"data": "value"}

    @app.get("/healthz")
    async def _healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def _readyz() -> dict[str, str]:
        return {"status": "ok"}

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# HTTP header values must be ASCII.  Use a fixed ASCII printable alphabet so
# Hypothesis never generates multi-byte Unicode characters that httpx would
# refuse to encode.
_ASCII_ALPHABET = st.sampled_from(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_!@#$%^&*"
)

# Non-empty ASCII strings used as secrets and header values.
_text = st.text(alphabet=_ASCII_ALPHABET, min_size=1, max_size=128)

# Header value that may be absent or an arbitrary ASCII string.
_maybe_header = st.one_of(
    st.none(),  # header absent
    st.text(alphabet=_ASCII_ALPHABET, min_size=0, max_size=128),
)


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


@given(secret=_text, sent_value=_maybe_header)
def test_non_health_endpoint_refused_when_header_missing_or_wrong(
    secret: str,
    sent_value: str | None,
) -> None:
    """Property 3: Origin guard.

    For any request whose X-Origin-Verify header is missing or differs from
    the secret, every non-health endpoint SHALL refuse it.
    Validates: Requirement 2.3
    """
    # Only applies when the sent value is not the exact secret.
    if sent_value == secret:
        return

    env_override = {**os.environ, "ORIGIN_VERIFY_SECRET": secret}
    with patch.dict(os.environ, env_override, clear=True):
        get_settings.cache_clear()
        client = _make_client()

        headers: dict[str, str] = {}
        if sent_value is not None:
            headers["X-Origin-Verify"] = sent_value

        response = client.get("/api/data", headers=headers)

    get_settings.cache_clear()

    assert response.status_code == 403, (
        f"Expected 403 for secret={secret!r} sent={sent_value!r}, got {response.status_code}"
    )


@given(secret=_text)
def test_non_health_endpoint_allowed_with_exact_secret(secret: str) -> None:
    """Property 3: Origin guard (complement).

    For any request whose X-Origin-Verify header exactly matches the secret,
    non-health endpoints SHALL be allowed through.
    Validates: Requirement 2.3
    """
    env_override = {**os.environ, "ORIGIN_VERIFY_SECRET": secret}
    with patch.dict(os.environ, env_override, clear=True):
        get_settings.cache_clear()
        client = _make_client()

        response = client.get("/api/data", headers={"X-Origin-Verify": secret})

    get_settings.cache_clear()

    assert response.status_code == 200, (
        f"Expected 200 for exact secret={secret!r}, got {response.status_code}"
    )


@given(secret=_text, sent_value=_maybe_header)
def test_health_endpoints_always_allowed(
    secret: str,
    sent_value: str | None,
) -> None:
    """Property 3: Origin guard (health exemption).

    Health endpoints (/healthz, /readyz) SHALL always be accessible regardless
    of the X-Origin-Verify header value.
    Validates: Requirement 2.3
    """
    env_override = {**os.environ, "ORIGIN_VERIFY_SECRET": secret}
    with patch.dict(os.environ, env_override, clear=True):
        get_settings.cache_clear()
        client = _make_client()

        headers: dict[str, str] = {}
        if sent_value is not None:
            headers["X-Origin-Verify"] = sent_value

        results = {
            path: client.get(path, headers=headers).status_code for path in ("/healthz", "/readyz")
        }

    get_settings.cache_clear()

    for path, code in results.items():
        assert code == 200, (
            f"Expected 200 on {path} for secret={secret!r} sent={sent_value!r}, got {code}"
        )
