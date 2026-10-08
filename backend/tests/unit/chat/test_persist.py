"""Persistence tests for the chat service save step (guardrailed-chat Task 4.3).

Task 4.3 replaces the ``_persist_exchange`` seam Task 4.2 left: it signs the
Exchange with an HMAC, writes it to ``datasets/{id}/chat/{iso_ts}-{uuid}.json``,
publishes ``chat.exchange.saved`` to EventBridge, and sets ``saved: true`` — or,
when the S3 put fails, leaves ``saved: false``, skips the event, and still
attaches the signature so the UI's Retry can replay the payload to the
retry-save endpoint (design "Error Handling"; Requirement 5.5).

These tests drive the **real** request flow (DB/Corpus/pre-check stubbed exactly
as in ``test_service.py``) with the process-wide instrumented client pointed at
a fake streaming model, so the ``done`` event carries the real persisted
Exchange. S3 runs on a moto backend; the EventBridge publisher is captured so no
real ``put_events`` is made and the event body can be asserted.

_Validates: Requirements 5.1, 5.5, 5.7_
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from app.chat import assembly, service, signing
from app.chat.corpus import Corpus, CorpusReview, EntityProfile
from app.chat.service import app
from app.core.ai import AiClient, set_ai_client
from app.core.config import get_settings
from app.storage import s3 as s3_mod
from fastapi.testclient import TestClient
from moto import mock_aws

from tests.support.dynamodb import ensure_rate_limit_table

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
    """moto S3 + rate-limit table, the signing secret, and a reset S3 client."""
    monkeypatch.setenv("RATE_LIMITS_TABLE", "rate-limits")
    monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", "rate-limits")
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", "")
    monkeypatch.setenv("S3_BUCKET", _BUCKET)
    monkeypatch.setenv("CHAT_SIGNING_SECRET", _SECRET)
    get_settings.cache_clear()
    s3_mod.reset_client()
    with mock_aws():
        ensure_rate_limit_table("rate-limits", region=_REGION)
        client = boto3.client("s3", region_name=_REGION)
        client.create_bucket(Bucket=_BUCKET)
        yield client
    get_settings.cache_clear()
    s3_mod.reset_client()


# ---------------------------------------------------------------------------
# Builders / fakes (mirror test_service.py)
# ---------------------------------------------------------------------------


def _corpus(version: int = 2) -> Corpus:
    return Corpus(
        dataset_id=_DS,
        version=version,
        entity=EntityProfile(name="Acme CRM", category="software"),
        reviews=(
            CorpusReview(id="r_0001", text="Great support.", rating=5, date="2026-01-01"),
            CorpusReview(id="r_0002", text="Slow to load.", rating=2, date="2026-01-02"),
        ),
        total_review_count=2,
    )


def _state() -> service._DatasetState:
    return service._DatasetState(
        active_version=2,
        archived=False,
        platform="g2",
        original_url="https://example.com/acme",
    )


class _FakeStream:
    def __init__(self, text: str) -> None:
        self._text = text

    def __iter__(self) -> Iterator[Any]:
        words = self._text.split(" ")
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=120, cache_read_input_tokens=100)
            ),
        )
        for index, word in enumerate(words):
            chunk = word if index == len(words) - 1 else f"{word} "
            yield SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text=chunk),
            )
        yield SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=7))


class _FakeMessages:
    def __init__(self, text: str) -> None:
        self._text = text

    def create(self, **kwargs: Any) -> Any:
        assert kwargs.get("stream") is True  # noqa: S101
        return _FakeStream(self._text)


class _FakeAnthropic:
    def __init__(self, text: str) -> None:
        self.messages = _FakeMessages(text)


def _install_fake_model(text: str) -> None:
    set_ai_client(AiClient(client=_FakeAnthropic(text)))


def _stub_flow(monkeypatch: pytest.MonkeyPatch, *, corpus: Corpus) -> None:
    monkeypatch.setattr(service, "_load_dataset_state", lambda _ds: _state())
    monkeypatch.setattr(service.corpus, "load_corpus", lambda _ds, _v: corpus)
    monkeypatch.setattr(
        service.assembly,
        "load_recent_exchanges",
        lambda _ds, **_kw: list[assembly.PriorExchange](),
    )
    monkeypatch.setattr(service.precheck, "run_in_background", lambda *_a, **_kw: SimpleNamespace())
    monkeypatch.setattr(service.precheck, "await_result", lambda _task, **_kw: None)


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture publish_event calls as the service imports it (module attribute)."""
    captured: list[dict[str, Any]] = []

    def _fake(detail_type: str, detail: dict[str, Any]) -> None:
        captured.append({"detail_type": detail_type, "detail": detail})

    monkeypatch.setattr(service, "publish_event", _fake)
    return captured


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for raw in body.split("\n\n"):
        block = raw.strip()
        if not block:
            continue
        name = ""
        data = ""
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        events.append((name, json.loads(data)))
    return events


def _post(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    """POST a chat request and return the final ``done`` Exchange payload."""
    with client.stream("POST", f"/api/chat/datasets/{_DS}", json=body) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    events = _parse_sse(text)
    assert events[-1][0] == "done"
    return events[-1][1]


# ---------------------------------------------------------------------------
# Save success — object shape, event, signature
# ---------------------------------------------------------------------------


class TestSaveSuccess:
    """A successful save writes the object, flags saved, and publishes the event."""

    def test_saved_object_written_at_iso_ts_key_with_full_metadata(
        self, monkeypatch: pytest.MonkeyPatch, _env: boto3.client
    ) -> None:
        """The Exchange is stored at ``chat/{asked_at}-{id}.json`` with its content."""
        _stub_flow(monkeypatch, corpus=_corpus())
        _capture_events(monkeypatch)
        _install_fake_model("Support is praised [r_0001].")
        client = TestClient(app)

        exchange = _post(client, {"question": "praise?", "conversation_id": _CONV})

        assert exchange["saved"] is True
        # Key uses the asked_at ISO timestamp prefix (chronological listing).
        expected_key = f"datasets/{_DS}/chat/{exchange['asked_at']}-{exchange['id']}.json"
        obj = _env.get_object(Bucket=_BUCKET, Key=expected_key)
        stored = json.loads(obj["Body"].read())

        # Stored object carries full metadata + conversation id + citation snippets.
        assert stored["dataset_id"] == _DS
        assert stored["data_version"] == 2
        assert stored["conversation_id"] == _CONV
        assert stored["question"] == "praise?"
        assert stored["citations"] == ["r_0001"]
        assert stored["citation_snippets"]["r_0001"]["text"] == "Great support."
        assert stored["scope"] == "in_scope"
        assert stored["saved"] is True

    def test_saved_object_contains_no_visitor_identifying_data(
        self, monkeypatch: pytest.MonkeyPatch, _env: boto3.client
    ) -> None:
        """Requirement 5.1: the only identifier is the conversation id."""
        _stub_flow(monkeypatch, corpus=_corpus())
        _capture_events(monkeypatch)
        _install_fake_model("ok [r_0002].")
        client = TestClient(app)

        exchange = _post(client, {"question": "load?", "conversation_id": _CONV})
        key = f"datasets/{_DS}/chat/{exchange['asked_at']}-{exchange['id']}.json"
        stored = json.loads(_env.get_object(Bucket=_BUCKET, Key=key)["Body"].read())

        forbidden = {"ip", "client_ip", "ip_hash", "user", "user_id", "visitor", "visitor_id"}
        assert forbidden.isdisjoint(stored.keys())

    def test_done_payload_carries_a_verifiable_hmac_signature(
        self, monkeypatch: pytest.MonkeyPatch, _env: boto3.client
    ) -> None:
        """The done payload is signed; the signature verifies against the secret."""
        _stub_flow(monkeypatch, corpus=_corpus())
        _capture_events(monkeypatch)
        _install_fake_model("Support is praised [r_0001].")
        client = TestClient(app)

        exchange = _post(client, {"question": "praise?", "conversation_id": _CONV})

        assert isinstance(exchange["signature"], str) and exchange["signature"]
        # Task 5.2 verifies with the same scheme and secret.
        assert signing.verify_exchange(exchange, _SECRET) is True
        # A different secret must not verify.
        assert signing.verify_exchange(exchange, "wrong-secret") is False

    def test_event_published_on_success_with_ids_only(
        self, monkeypatch: pytest.MonkeyPatch, _env: boto3.client
    ) -> None:
        """Requirement 5.7: chat.exchange.saved is published with IDs, no content."""
        _stub_flow(monkeypatch, corpus=_corpus())
        events = _capture_events(monkeypatch)
        _install_fake_model("Support is praised [r_0001].")
        client = TestClient(app)

        exchange = _post(client, {"question": "praise?", "conversation_id": _CONV})

        assert len(events) == 1
        assert events[0]["detail_type"] == "chat.exchange.saved"
        detail = events[0]["detail"]
        # IDs only — no question/answer/snippets in the event body.
        assert detail["dataset_id"] == _DS
        assert detail["exchange_id"] == exchange["id"]
        assert detail["key"].endswith(f"{exchange['id']}.json")
        assert "question" not in detail
        assert "answer" not in detail
        assert "citation_snippets" not in detail


# ---------------------------------------------------------------------------
# Save failure — saved:false, no event, signature still present
# ---------------------------------------------------------------------------


class TestSaveFailure:
    """When the S3 put fails the answer is kept, no event fires, signature stays."""

    def test_save_failure_sets_saved_false_keeps_signature_no_event(
        self, monkeypatch: pytest.MonkeyPatch, _env: boto3.client
    ) -> None:
        _stub_flow(monkeypatch, corpus=_corpus())
        events = _capture_events(monkeypatch)
        _install_fake_model("Support is praised [r_0001].")

        # Force the S3 put to fail after the signature is attached.
        def _boom(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("s3 unavailable")

        monkeypatch.setattr(service.s3, "put_bytes", _boom)
        client = TestClient(app)

        exchange = _post(client, {"question": "praise?", "conversation_id": _CONV})

        # Answer is kept (done event still arrived) with saved:false.
        assert exchange["saved"] is False
        assert exchange["answer"] == "Support is praised [r_0001]."
        # Signature is present so the client can call the retry-save endpoint.
        assert signing.verify_exchange(exchange, _SECRET) is True
        # No event published on failure.
        assert events == []


# ---------------------------------------------------------------------------
# Signing scheme (unit-level, so Task 5.2 can match it)
# ---------------------------------------------------------------------------


class TestSigningScheme:
    """The canonical HMAC scheme is deterministic and tamper-evident."""

    def _exchange(self) -> dict[str, Any]:
        return {
            "id": "e1",
            "dataset_id": _DS,
            "data_version": 2,
            "conversation_id": _CONV,
            "question": "q",
            "answer": "a [r_0001]",
            "citations": ["r_0001"],
            "scope": "in_scope",
            "saved": False,
        }

    def test_signature_is_deterministic(self) -> None:
        ex = self._exchange()
        assert signing.sign_exchange(ex, _SECRET) == signing.sign_exchange(ex, _SECRET)

    def test_saved_flag_does_not_change_the_signature(self) -> None:
        """``saved`` is excluded, so flipping it after signing stays verifiable."""
        ex = self._exchange()
        ex["signature"] = signing.sign_exchange(ex, _SECRET)
        ex["saved"] = True
        assert signing.verify_exchange(ex, _SECRET) is True

    def test_key_order_does_not_change_the_signature(self) -> None:
        ex = self._exchange()
        reordered = {k: ex[k] for k in reversed(list(ex.keys()))}
        assert signing.sign_exchange(ex, _SECRET) == signing.sign_exchange(reordered, _SECRET)

    def test_edited_content_fails_verification(self) -> None:
        ex = self._exchange()
        ex["signature"] = signing.sign_exchange(ex, _SECRET)
        ex["answer"] = "forged answer"
        assert signing.verify_exchange(ex, _SECRET) is False

    def test_missing_signature_fails_verification(self) -> None:
        assert signing.verify_exchange(self._exchange(), _SECRET) is False
