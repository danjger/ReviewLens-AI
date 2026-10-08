"""Unit/route tests for ``POST /uploads`` (dataset-ingestion task 8.1).

These exercise the upload route in :mod:`app.ingestion.api` through a FastAPI
``TestClient``. They assert the HTTP contract from the design's "API endpoints"
and "Error Handling" tables and Requirement 7.1:

- happy path → ``201`` with ``{upload_id, put_url, expires_at}`` and a pre-signed
  PUT for a key under ``uploads/{upload_id}/`` (verified against the real moto
  presigner so the signed Content-Length/Content-Type conditions are exercised);
- an over-limit ``size_bytes`` → ``422`` *before* any URL is issued (the presign
  helper is never called);
- a non ``.csv``/``.tsv`` extension → ``422``;
- a rate-limit hit → ``429`` with the ``RATE_LIMIT_EXCEEDED`` code and a numeric
  ``Retry-After`` header; nothing is presigned.

The rate limiter and the client-IP extraction are faked at the router's module
boundary (same style as ``test_check_api.py``). The happy-path test uses moto so
the pre-signed URL is a genuine S3 URL built from ``keys.upload_file`` rather
than a stub, which also confirms the key layout.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import boto3
import pytest
from app.core.config import get_settings
from app.core.errors import RateLimitError
from app.ingestion import api as api_mod
from app.storage import s3 as s3_mod
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

_BUCKET = "reviewlens-test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def wiring(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the limiter and client-IP; record rate-limit calls.

    The presign helper is left real so the happy-path test (under moto) sees a
    genuine S3 URL. The limiter is faked so no DynamoDB is needed; by default it
    allows the request (records the call and returns ``None``).
    """
    rate_calls: list[tuple[str, str | None, int]] = []

    monkeypatch.setattr(
        api_mod,
        "check_rate_limit",
        lambda action, ip, limit, **kw: rate_calls.append((action, ip, limit)),
    )
    # No CloudFront header in tests → the limiter is called with ip=None.
    monkeypatch.setattr(api_mod, "get_client_ip", lambda request: None)
    return {"rate_calls": rate_calls}


@pytest.fixture()
def client(wiring: dict[str, Any]) -> TestClient:
    """A TestClient for an app mounting only the uploads router.

    Mirrors app.api wiring (router under /api + the shared error handlers and the
    Retry-After header) without the origin-guard middleware.
    """
    from app.core.errors import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)
    api_mod.register_rate_limit_header(app)
    app.include_router(api_mod.uploads_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def s3_bucket(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A moto-backed S3 bucket so the presigner builds a real URL.

    Clears the cached settings and S3 client so ``S3_BUCKET`` and the fresh moto
    client take effect, and resets them again on teardown.
    """
    monkeypatch.setenv("S3_BUCKET", _BUCKET)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "")
    get_settings.cache_clear()
    s3_mod.reset_client()
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=_BUCKET)
        yield
    get_settings.cache_clear()
    s3_mod.reset_client()


_GOOD_BODY = {
    "filename": "reviews.csv",
    "size_bytes": 1024,
    "content_type": "text/csv",
}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_create_upload_returns_201_with_presigned_put(
    client: TestClient, wiring: dict[str, Any], s3_bucket: Any
) -> None:
    resp = client.post("/api/uploads", json=_GOOD_BODY)
    assert resp.status_code == 201
    body = resp.json()

    # Shape from the design's API-endpoints table.
    assert set(body.keys()) == {"upload_id", "put_url", "expires_at"}
    assert body["upload_id"]
    assert body["expires_at"]

    # The pre-signed URL targets the key uploads/{upload_id}/file.
    path = urlparse(body["put_url"]).path
    assert path.endswith(f"uploads/{body['upload_id']}/file")
    # It is a signed URL (query string carries the signature params).
    assert "Signature" in body["put_url"] or "X-Amz-Signature" in body["put_url"]


def test_create_upload_accepts_tsv_extension(
    client: TestClient, wiring: dict[str, Any], s3_bucket: Any
) -> None:
    resp = client.post(
        "/api/uploads",
        json={
            "filename": "export.TSV",
            "size_bytes": 2048,
            "content_type": "text/tab-separated-values",
        },
    )
    assert resp.status_code == 201


def test_create_upload_rate_limit_uses_uploads_action(
    client: TestClient, wiring: dict[str, Any], s3_bucket: Any
) -> None:
    client.post("/api/uploads", json=_GOOD_BODY)
    assert wiring["rate_calls"] == [("uploads", None, 30)]


# ---------------------------------------------------------------------------
# Over-limit → 422 (refused before any URL is issued)
# ---------------------------------------------------------------------------


def test_create_upload_over_limit_is_422_without_presigning(
    client: TestClient, wiring: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_MB", "10")
    get_settings.cache_clear()
    presign_calls: list[Any] = []
    monkeypatch.setattr(
        api_mod.s3,
        "presign_put",
        lambda *a, **kw: presign_calls.append((a, kw)) or "should-not-be-used",
    )

    over = {"filename": "big.csv", "size_bytes": 10 * 1024 * 1024 + 1, "content_type": "text/csv"}
    resp = client.post("/api/uploads", json=over)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    # No pre-signed URL was issued for an over-limit file.
    assert presign_calls == []
    get_settings.cache_clear()


def test_create_upload_at_limit_is_allowed(
    client: TestClient, wiring: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_MB", "10")
    get_settings.cache_clear()
    monkeypatch.setattr(api_mod.s3, "presign_put", lambda *a, **kw: "https://signed.example/put")

    exactly = {"filename": "edge.csv", "size_bytes": 10 * 1024 * 1024, "content_type": "text/csv"}
    resp = client.post("/api/uploads", json=exactly)

    assert resp.status_code == 201
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Bad extension → 422
# ---------------------------------------------------------------------------


def test_create_upload_bad_extension_is_422(
    client: TestClient, wiring: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    presign_calls: list[Any] = []
    monkeypatch.setattr(
        api_mod.s3,
        "presign_put",
        lambda *a, **kw: presign_calls.append((a, kw)) or "should-not-be-used",
    )
    resp = client.post(
        "/api/uploads",
        json={"filename": "notes.txt", "size_bytes": 100, "content_type": "text/plain"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert presign_calls == []


# ---------------------------------------------------------------------------
# Malformed request bodies → 422 (request validation)
# ---------------------------------------------------------------------------


def test_create_upload_zero_size_is_422(client: TestClient, wiring: dict[str, Any]) -> None:
    resp = client.post(
        "/api/uploads",
        json={"filename": "x.csv", "size_bytes": 0, "content_type": "text/csv"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_upload_missing_fields_is_422(client: TestClient, wiring: dict[str, Any]) -> None:
    resp = client.post("/api/uploads", json={"filename": "x.csv"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Rate limited → 429
# ---------------------------------------------------------------------------


def test_create_upload_rate_limited_is_429(
    client: TestClient, wiring: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(action: str, ip: str | None, limit: int, **kw: Any) -> None:
        raise RateLimitError(
            "Rate limit exceeded. Retry after 42 seconds.",
            code="RATE_LIMIT_EXCEEDED",
            status_code=429,
        )

    monkeypatch.setattr(api_mod, "check_rate_limit", _raise)
    presign_calls: list[Any] = []
    monkeypatch.setattr(
        api_mod.s3,
        "presign_put",
        lambda *a, **kw: presign_calls.append((a, kw)) or "should-not-be-used",
    )

    resp = client.post("/api/uploads", json=_GOOD_BODY)

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert resp.headers["Retry-After"] == "42"
    # Nothing presigned when the limit is hit.
    assert presign_calls == []
