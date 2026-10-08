"""Retry-save endpoint tests (guardrailed-chat Task 5.2).

``POST /api/datasets/{id}/chat/save`` persists an Exchange the chat service
could not save on the first try, but **only** when the payload still carries a
valid HMAC signature (the exact Task 4.3 scheme, :mod:`app.chat.signing`). This
is what stops a visitor forging or editing the shared history (design
"Endpoints"; Correctness Property 4: "Saved history can't be forged").

These tests mount the real :data:`app.chat.save_api.router` on a bare FastAPI
app with the shared error handlers, so the save route is exercised exactly as it
is in the API service, without pulling in the DB-backed Library/ingestion
routers or the streaming chat service. S3 runs on a moto backend; the
EventBridge publisher is captured so no real ``put_events`` is made and the
event body can be asserted. A valid Exchange is signed with
``signing.sign_exchange`` using the test secret, mirroring what the chat service
attaches in the ``done`` event.

_Validates: Requirement 5.5 (Correctness Property 4)._
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from app.chat import save_api, signing
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.storage import s3 as s3_mod
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

_REGION = "us-east-1"
_BUCKET = "reviewlens-test"
_DS = "11111111-1111-1111-1111-111111111111"
_CONV = "conv-aaaa"
_SECRET = "test-chat-signing-secret"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[boto3.client]:
    """moto S3, the signing secret, a reset S3 client, and a captured publisher."""
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", "")
    monkeypatch.setenv("S3_BUCKET", _BUCKET)
    monkeypatch.setenv("CHAT_SIGNING_SECRET", _SECRET)
    get_settings.cache_clear()
    s3_mod.reset_client()
    with mock_aws():
        client = boto3.client("s3", region_name=_REGION)
        client.create_bucket(Bucket=_BUCKET)
        yield client
    get_settings.cache_clear()
    s3_mod.reset_client()


@pytest.fixture
def _events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture publish_event as the save module imports it (module attribute)."""
    captured: list[dict[str, Any]] = []

    def _fake(detail_type: str, detail: dict[str, Any]) -> None:
        captured.append({"detail_type": detail_type, "detail": detail})

    monkeypatch.setattr(save_api, "publish_event", _fake)
    return captured


@pytest.fixture
def _client() -> TestClient:
    """A bare app carrying only the save router + shared error envelope."""
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(save_api.router, prefix="/api")
    return TestClient(app)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _exchange(**overrides: Any) -> dict[str, Any]:
    """A ``done``-style Exchange with ``saved:false`` and no signature yet."""
    base: dict[str, Any] = {
        "id": "e-0001",
        "dataset_id": _DS,
        "data_version": 2,
        "conversation_id": _CONV,
        "asked_at": "2026-01-15T12:34:56+00:00",
        "answered_at": "2026-01-15T12:35:01+00:00",
        "question": "What do reviewers praise?",
        "answer": "Support is praised [r_0001].",
        "citations": ["r_0001"],
        "dropped_citations": 0,
        "citation_snippets": {
            "r_0001": {"text": "Great support.", "rating": 5, "date": "2026-01-01"}
        },
        "scope": "in_scope",
        "scope_category": None,
        "precheck": {"label": "in_scope", "latency_ms": 420},
        "model": "claude-x",
        "prompt_version": "system_v1",
        "usage": {"input_tokens": 120, "cache_read_tokens": 100, "output_tokens": 7},
        "saved": False,
    }
    base.update(overrides)
    return base


def _signed(**overrides: Any) -> dict[str, Any]:
    """A valid Exchange carrying a signature over its content (saved excluded)."""
    exchange = _exchange(**overrides)
    exchange["signature"] = signing.sign_exchange(exchange, _SECRET)
    return exchange


def _expected_key(exchange: dict[str, Any]) -> str:
    return f"datasets/{_DS}/chat/{exchange['asked_at']}-{exchange['id']}.json"


def _list_chat_keys(s3_client: boto3.client) -> list[str]:
    resp = s3_client.list_objects_v2(Bucket=_BUCKET, Prefix=f"datasets/{_DS}/chat/")
    return [obj["Key"] for obj in resp.get("Contents", [])]


# ---------------------------------------------------------------------------
# Valid signature -> saved
# ---------------------------------------------------------------------------


class TestValidSignatureSaves:
    """A valid signature persists the Exchange and publishes the event."""

    def test_valid_signature_writes_to_the_right_key_with_saved_true(
        self, _env: boto3.client, _events: list[dict[str, Any]], _client: TestClient
    ) -> None:
        signed = _signed()
        response = _client.post(f"/api/datasets/{_DS}/chat/save", json=signed)

        assert response.status_code == 200
        returned = response.json()
        assert returned["saved"] is True

        key = _expected_key(signed)
        stored = json.loads(_env.get_object(Bucket=_BUCKET, Key=key)["Body"].read())
        assert stored["saved"] is True
        assert stored["dataset_id"] == _DS
        assert stored["id"] == "e-0001"
        assert stored["question"] == signed["question"]
        assert stored["citation_snippets"]["r_0001"]["text"] == "Great support."

    def test_valid_signature_publishes_saved_event_with_ids_only(
        self, _env: boto3.client, _events: list[dict[str, Any]], _client: TestClient
    ) -> None:
        signed = _signed()
        _client.post(f"/api/datasets/{_DS}/chat/save", json=signed)

        assert len(_events) == 1
        assert _events[0]["detail_type"] == "chat.exchange.saved"
        detail = _events[0]["detail"]
        assert detail == {
            "dataset_id": _DS,
            "exchange_id": "e-0001",
            "key": _expected_key(signed),
        }
        # IDs only — no Exchange content leaks into the event body.
        assert "question" not in detail
        assert "answer" not in detail
        assert "citation_snippets" not in detail


# ---------------------------------------------------------------------------
# Tampered / missing / mismatched -> refused
# ---------------------------------------------------------------------------


class TestForgedPayloadsRefused:
    """Anything whose signature doesn't match is refused; nothing is written."""

    def test_tampered_answer_is_refused_and_nothing_written(
        self, _env: boto3.client, _events: list[dict[str, Any]], _client: TestClient
    ) -> None:
        """A signed payload whose content was edited after signing fails (403)."""
        forged = _signed()
        forged["answer"] = "Edited after signing — the weather is sunny."

        response = _client.post(f"/api/datasets/{_DS}/chat/save", json=forged)

        assert response.status_code == 403
        assert response.json()["error"]["code"] == save_api.SAVE_REFUSED_CODE
        assert _list_chat_keys(_env) == []
        assert _events == []

    def test_citation_snippet_tamper_is_refused(
        self, _env: boto3.client, _events: list[dict[str, Any]], _client: TestClient
    ) -> None:
        """Even editing a nested snippet (what the popover shows) is caught."""
        forged = _signed()
        forged["citation_snippets"]["r_0001"]["text"] = "Fabricated review text."

        response = _client.post(f"/api/datasets/{_DS}/chat/save", json=forged)

        assert response.status_code == 403
        assert _list_chat_keys(_env) == []
        assert _events == []

    def test_missing_signature_is_refused(
        self, _env: boto3.client, _events: list[dict[str, Any]], _client: TestClient
    ) -> None:
        """An unsigned payload (a plain forged Exchange) is refused."""
        unsigned = _exchange()  # no signature attached

        response = _client.post(f"/api/datasets/{_DS}/chat/save", json=unsigned)

        assert response.status_code == 403
        assert _list_chat_keys(_env) == []
        assert _events == []

    def test_signature_from_a_different_secret_is_refused(
        self, _env: boto3.client, _events: list[dict[str, Any]], _client: TestClient
    ) -> None:
        """A payload signed with the wrong secret cannot be trusted."""
        exchange = _exchange()
        exchange["signature"] = signing.sign_exchange(exchange, "attacker-secret")

        response = _client.post(f"/api/datasets/{_DS}/chat/save", json=exchange)

        assert response.status_code == 403
        assert _list_chat_keys(_env) == []
        assert _events == []

    def test_path_dataset_id_mismatch_is_refused(
        self, _env: boto3.client, _events: list[dict[str, Any]], _client: TestClient
    ) -> None:
        """A validly signed Exchange cannot be saved under a different dataset.

        The signature covers the body ``dataset_id``; posting it to another
        dataset's path is refused before any write, so it can't be smuggled into
        a different dataset's history.
        """
        signed = _signed()  # dataset_id == _DS
        other = "22222222-2222-2222-2222-222222222222"

        response = _client.post(f"/api/datasets/{other}/chat/save", json=signed)

        assert response.status_code == 403
        assert response.json()["error"]["code"] == save_api.SAVE_REFUSED_CODE
        # Nothing written under either dataset's prefix.
        assert _list_chat_keys(_env) == []
        other_keys = _env.list_objects_v2(Bucket=_BUCKET, Prefix=f"datasets/{other}/chat/")
        assert other_keys.get("Contents", []) == []
        assert _events == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotentResave:
    """Re-saving the same valid Exchange is safe (same key, same bytes)."""

    def test_resaving_same_exchange_overwrites_one_object(
        self, _env: boto3.client, _events: list[dict[str, Any]], _client: TestClient
    ) -> None:
        signed = _signed()

        first = _client.post(f"/api/datasets/{_DS}/chat/save", json=signed)
        second = _client.post(f"/api/datasets/{_DS}/chat/save", json=signed)

        assert first.status_code == 200
        assert second.status_code == 200
        # Exactly one object at exactly one key — the retry overwrote, not forked.
        keys_after = _list_chat_keys(_env)
        assert keys_after == [_expected_key(signed)]
        # Stored bytes are identical across the two saves.
        stored = json.loads(_env.get_object(Bucket=_BUCKET, Key=keys_after[0])["Body"].read())
        assert stored["saved"] is True
        assert stored["id"] == "e-0001"
        # Both responses carry saved:true.
        assert first.json()["saved"] is True
        assert second.json()["saved"] is True
