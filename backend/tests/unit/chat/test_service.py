"""Service tests for the streaming chat app (guardrailed-chat Task 4.1 + 4.2).

Task 4.1 built the service skeleton (origin guard, rate limit, SSE streaming in
both compute modes). Task 4.2 replaced the placeholder token source with the
real request flow (design "Architecture"):

1. origin guard + rate limit (reused; a 429 is a pre-stream HTTP error);
2. **availability guard** — ``409 CHAT_UNAVAILABLE`` when the dataset has no
   ``active_version`` or is archived (Requirements 1.1/1.2/1.3);
3. **length validation** — ``422 INVALID_QUESTION`` for empty / whitespace-only
   / over-1,000-char questions (Requirement 6.1);
4. parallel Corpus load + scope pre-check, then message assembly;
5. the **streaming Claude call** through the instrumented client → SSE
   ``token`` events (Requirement 7.1);
6. post-processing (citations → prompt-leak → scope tag);
7. the terminal ``done`` event with the Exchange payload;
8. a mid-stream model error → SSE ``error`` event, nothing saved.

These tests keep the DB and S3 collaborators stubbed (the service's
``_load_dataset_state``, ``corpus.load_corpus``, and
``assembly.load_recent_exchanges`` are monkeypatched) and drive the model
through a controllable fake streaming client wrapped in the **real**
instrumented :class:`AiClient`, so the flow — including the global AI-call limit
and SSE framing — is exercised offline. The rate-limit path still writes to the
``rate-limits`` DynamoDB table, so these tests run against a moto backend.

_Validates: Requirements 1.1, 1.2, 1.3, 6.1, 7.1_
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from app.chat import assembly, service
from app.chat.corpus import Corpus, CorpusReview, EntityProfile
from app.chat.precheck import PrecheckResult
from app.chat.service import app
from app.core.ai import AiClient, set_ai_client
from app.core.config import get_settings
from fastapi.testclient import TestClient
from moto import mock_aws

from tests.support.dynamodb import ensure_rate_limit_table

_SECRET = "cloudfront-origin-secret"
_REGION = "us-east-1"
_DS = "11111111-1111-1111-1111-111111111111"
_CONV = "conv-aaaa"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _dynamodb_and_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Create the rate-limit table in moto and reset the memoised settings."""
    monkeypatch.setenv("RATE_LIMITS_TABLE", "rate-limits")
    monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", "rate-limits")
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", "")  # local/container bypass by default
    get_settings.cache_clear()
    with mock_aws():
        ensure_rate_limit_table("rate-limits", region=_REGION)
        yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Builders / fakes
# ---------------------------------------------------------------------------


def _corpus(version: int = 2) -> Corpus:
    """A tiny two-review Corpus whose IDs the fake answer can cite."""
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


def _state(
    *,
    active_version: int | None = 2,
    archived: bool = False,
) -> service._DatasetState:
    return service._DatasetState(
        active_version=active_version,
        archived=archived,
        platform="g2",
        original_url="https://example.com/acme",
    )


class _FakeStream:
    """An iterable of SDK-shaped stream events built from text word chunks.

    Optionally raises *raise_after* text chunks have been yielded, to simulate a
    provider error mid-stream (design "Error Handling").
    """

    def __init__(self, text: str, *, raise_after: int | None = None) -> None:
        self._text = text
        self._raise_after = raise_after

    def __iter__(self) -> Iterator[Any]:
        words = self._text.split(" ")
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=120, cache_read_input_tokens=100)
            ),
        )
        emitted = 0
        for index, word in enumerate(words):
            chunk = word if index == len(words) - 1 else f"{word} "
            yield SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text=chunk),
            )
            emitted += 1
            if self._raise_after is not None and emitted >= self._raise_after:
                raise RuntimeError("provider exploded mid-stream")
        yield SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=7))


class _FakeMessages:
    def __init__(self, text: str, *, raise_after: int | None = None) -> None:
        self._text = text
        self._raise_after = raise_after

    def create(self, **kwargs: Any) -> Any:
        assert kwargs.get("stream") is True  # noqa: S101 - the chat uses streaming
        return _FakeStream(self._text, raise_after=self._raise_after)


class _FakeAnthropic:
    def __init__(self, text: str, *, raise_after: int | None = None) -> None:
        self.messages = _FakeMessages(text, raise_after=raise_after)


def _install_fake_model(text: str, *, raise_after: int | None = None) -> None:
    """Point the process-wide instrumented client at a fake streaming model."""
    set_ai_client(AiClient(client=_FakeAnthropic(text, raise_after=raise_after)))


def _stub_flow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: service._DatasetState | None,
    corpus: Corpus | None = None,
    history: list[assembly.PriorExchange] | None = None,
    precheck: PrecheckResult | None = None,
) -> dict[str, Any]:
    """Stub the DB/S3/pre-check collaborators; capture the assembled request.

    Returns a dict the test can read after the call: ``assembled`` holds the
    :class:`AssembledMessages` the flow built, so a test can assert the pre-check
    hint reached assembly.
    """
    captured: dict[str, Any] = {}

    monkeypatch.setattr(service, "_load_dataset_state", lambda _ds: state)
    if corpus is not None:
        monkeypatch.setattr(service.corpus, "load_corpus", lambda _ds, _v: corpus)
    monkeypatch.setattr(
        service.assembly,
        "load_recent_exchanges",
        lambda _ds, **_kw: list(history or []),
    )

    # The pre-check runs on a thread; stub both halves so no AI call is made and
    # the result is deterministic. ``run_in_background`` returns a sentinel task.
    monkeypatch.setattr(
        service.precheck,
        "run_in_background",
        lambda *_a, **_kw: SimpleNamespace(),
    )
    monkeypatch.setattr(service.precheck, "await_result", lambda _task, **_kw: precheck)

    real_assemble = service.assembly.assemble_messages

    def _capture_assemble(**kwargs: Any) -> Any:
        result = real_assemble(**kwargs)
        captured["assembled"] = result
        captured["precheck_hint"] = kwargs.get("precheck")
        return result

    monkeypatch.setattr(service.assembly, "assemble_messages", _capture_assemble)
    return captured


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE stream body into a list of (event name, data) tuples."""
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


def _post(client: TestClient, body: dict[str, Any]) -> str:
    """POST a chat request and return the full streamed SSE body."""
    with client.stream("POST", f"/api/chat/datasets/{_DS}", json=body) as response:
        assert response.status_code == 200
        return "".join(response.iter_text())


# ---------------------------------------------------------------------------
# Origin guard (Task 4.1 — kept)
# ---------------------------------------------------------------------------


class TestOriginGuard:
    """A request that did not come through CloudFront is refused."""

    def test_missing_origin_header_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORIGIN_VERIFY_SECRET", _SECRET)
        get_settings.cache_clear()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(f"/api/chat/datasets/{_DS}", json={"question": "hi"})

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    def test_wrong_origin_header_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORIGIN_VERIFY_SECRET", _SECRET)
        get_settings.cache_clear()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            f"/api/chat/datasets/{_DS}",
            json={"question": "hi"},
            headers={"X-Origin-Verify": "not-the-secret"},
        )

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Availability guard (Task 4.2) — Requirements 1.1, 1.2, 1.3
# ---------------------------------------------------------------------------


class TestAvailabilityGuard:
    """409 CHAT_UNAVAILABLE for no active version, unknown id, or archived."""

    def test_no_active_version_returns_409(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_flow(monkeypatch, state=_state(active_version=None))
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(f"/api/chat/datasets/{_DS}", json={"question": "hi"})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CHAT_UNAVAILABLE"

    def test_unknown_dataset_returns_409(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_flow(monkeypatch, state=None)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(f"/api/chat/datasets/{_DS}", json={"question": "hi"})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CHAT_UNAVAILABLE"

    def test_archived_returns_409(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_flow(monkeypatch, state=_state(archived=True))
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(f"/api/chat/datasets/{_DS}", json={"question": "hi"})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CHAT_UNAVAILABLE"

    def test_refresh_in_flight_still_answers_from_active_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dataset with an active version answers even if a refresh is running.

        The guard blocks only on *no* active version or *archived*; a refresh in
        flight keeps the current ``active_version`` live (Requirement 1.1). We
        model that as a dataset with ``active_version=2`` (not archived) and
        assert the request streams a normal answer.
        """
        _stub_flow(monkeypatch, state=_state(active_version=2), corpus=_corpus(version=2))
        _install_fake_model("Support is praised [r_0001].")
        client = TestClient(app)

        body = _post(client, {"question": "what is good?", "conversation_id": _CONV})
        events = _parse_sse(body)

        assert events[-1][0] == "done"
        assert events[-1][1]["data_version"] == 2


# ---------------------------------------------------------------------------
# Length validation (Task 4.2) — Requirement 6.1
# ---------------------------------------------------------------------------


class TestLengthValidation:
    """422 INVALID_QUESTION for empty / whitespace-only / over-length questions."""

    def test_empty_question_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_flow(monkeypatch, state=_state())
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(f"/api/chat/datasets/{_DS}", json={"question": ""})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_QUESTION"

    def test_whitespace_only_question_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_flow(monkeypatch, state=_state())
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(f"/api/chat/datasets/{_DS}", json={"question": "   \n\t "})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_QUESTION"

    def test_over_length_question_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_flow(monkeypatch, state=_state())
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            f"/api/chat/datasets/{_DS}",
            json={"question": "x" * (service.MAX_QUESTION_CHARS + 1)},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_QUESTION"

    def test_question_at_limit_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exactly 1,000 characters is allowed (boundary)."""
        _stub_flow(monkeypatch, state=_state(), corpus=_corpus())
        _install_fake_model("ok")
        client = TestClient(app)

        body = _post(client, {"question": "x" * service.MAX_QUESTION_CHARS})

        assert _parse_sse(body)[-1][0] == "done"


# ---------------------------------------------------------------------------
# Streaming + done payload (Task 4.2) — Requirement 7.1
# ---------------------------------------------------------------------------


class TestStreamingAndDone:
    """Token events stream incrementally, then a done event with the Exchange."""

    def test_streams_tokens_then_done_with_exchange(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_flow(monkeypatch, state=_state(), corpus=_corpus())
        _install_fake_model("The most common praise is support [r_0001].")
        client = TestClient(app)

        body = _post(client, {"question": "top praise?", "conversation_id": _CONV})
        events = _parse_sse(body)
        names = [name for name, _ in events]

        # Multiple token events then a terminal done event.
        assert names.count("token") > 1
        assert names[-1] == "done"
        # Concatenated tokens reconstruct the raw answer.
        tokens = "".join(d["text"] for n, d in events if n == "token")
        assert tokens == "The most common praise is support [r_0001]."

        exchange = events[-1][1]
        assert exchange["dataset_id"] == _DS
        assert exchange["data_version"] == 2
        assert exchange["conversation_id"] == _CONV
        assert exchange["question"] == "top praise?"
        assert exchange["answer"] == "The most common praise is support [r_0001]."
        # The one valid citation is kept with a snippet (Requirement 2.2/2.5).
        assert exchange["citations"] == ["r_0001"]
        assert exchange["citation_snippets"]["r_0001"]["text"] == "Great support."
        assert exchange["scope"] == "in_scope"
        # Task 4.3 seam: not yet persisted.
        assert exchange["saved"] is False
        # Usage was collected from the stream.
        assert exchange["usage"]["output_tokens"] == 7
        assert exchange["usage"]["cache_read_tokens"] == 100
        assert exchange["model"] == get_settings().claude_chat_model

    def test_invalid_citation_is_dropped_from_the_exchange(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A citation to an ID not in the Corpus is removed (Requirement 2.5)."""
        _stub_flow(monkeypatch, state=_state(), corpus=_corpus())
        _install_fake_model("Praise [r_0001] and a phantom [r_9999].")
        client = TestClient(app)

        body = _post(client, {"question": "praise?", "conversation_id": _CONV})
        exchange = _parse_sse(body)[-1][1]

        assert exchange["citations"] == ["r_0001"]
        assert exchange["dropped_citations"] == 1

    def test_missing_conversation_id_is_minted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A request with no conversation_id still saves with a minted one."""
        _stub_flow(monkeypatch, state=_state(), corpus=_corpus())
        _install_fake_model("ok [r_0002].")
        client = TestClient(app)

        body = _post(client, {"question": "load speed?"})
        exchange = _parse_sse(body)[-1][1]

        assert exchange["conversation_id"]  # non-empty minted id


# ---------------------------------------------------------------------------
# Pre-check hint flows into assembly (Task 4.2)
# ---------------------------------------------------------------------------


class TestPrecheckHint:
    """A flagged pre-check becomes a hint passed to message assembly."""

    def test_out_of_scope_precheck_hint_reaches_assembly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _stub_flow(
            monkeypatch,
            state=_state(),
            corpus=_corpus(),
            precheck=PrecheckResult(label="out_of_scope", category="weather"),
        )
        _install_fake_model("That's outside what I can answer here.")
        client = TestClient(app)

        _post(client, {"question": "weather?", "conversation_id": _CONV})

        hint = captured["precheck_hint"]
        assert hint is not None
        assert hint.label == "out_of_scope"
        assert hint.category == "weather"
        # The hint is rendered into the assembled question turn.
        last_turn = captured["assembled"].messages[-1]["content"]
        assert "<precheck>likely out_of_scope: weather</precheck>" in last_turn

    def test_in_scope_precheck_adds_no_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _stub_flow(
            monkeypatch,
            state=_state(),
            corpus=_corpus(),
            precheck=PrecheckResult(label="in_scope"),
        )
        _install_fake_model("Support is praised [r_0001].")
        client = TestClient(app)

        _post(client, {"question": "praise?", "conversation_id": _CONV})

        assert captured["precheck_hint"] is None

    def test_timed_out_precheck_adds_no_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the pre-check times out (await_result → None) no hint is added."""
        captured = _stub_flow(monkeypatch, state=_state(), corpus=_corpus(), precheck=None)
        _install_fake_model("Support is praised [r_0001].")
        client = TestClient(app)

        _post(client, {"question": "praise?", "conversation_id": _CONV})

        assert captured["precheck_hint"] is None


# ---------------------------------------------------------------------------
# Mid-stream error (Task 4.2) — design "Error Handling"
# ---------------------------------------------------------------------------


class TestMidStreamError:
    """A provider error mid-stream emits an SSE error event; nothing is saved."""

    def test_error_mid_stream_emits_error_event_no_done(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_flow(monkeypatch, state=_state(), corpus=_corpus())
        # Yield one token, then raise.
        _install_fake_model("partial answer here", raise_after=1)
        client = TestClient(app)

        body = _post(client, {"question": "what?", "conversation_id": _CONV})
        events = _parse_sse(body)
        names = [name for name, _ in events]

        # Partial token(s) streamed, then an error event, and NO done event.
        assert "token" in names
        assert names[-1] == "error"
        assert "done" not in names
        error_payload = events[-1][1]
        assert error_payload["code"] == "ANSWER_INTERRUPTED"


# ---------------------------------------------------------------------------
# Both compute modes still stream (Task 4.1 — kept, adapted to the real flow)
# ---------------------------------------------------------------------------


class TestLambdaModeStreaming:
    """The LWA relies on the ASGI body arriving in multiple chunks.

    Driving the app directly over ASGI asserts that the real flow still emits the
    SSE body incrementally (multiple ``http.response.body`` messages), which is
    what lets the Function URL stream token by token in Lambda mode.
    """

    @staticmethod
    async def _drive_asgi() -> tuple[int, list[bytes], bool]:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "path": f"/api/chat/datasets/{_DS}",
            "raw_path": f"/api/chat/datasets/{_DS}".encode(),
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "headers": [(b"content-type", b"application/json")],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
        }
        payload = {"question": "alpha beta gamma", "conversation_id": _CONV}
        request_body = json.dumps(payload).encode()
        sent = {"done": False}

        async def receive() -> dict:
            if sent["done"]:
                await asyncio.Event().wait()
            sent["done"] = True
            return {"type": "http.request", "body": request_body, "more_body": False}

        status_code = 0
        body_chunks: list[bytes] = []
        body_message_count = 0

        async def send(message: dict) -> None:
            nonlocal status_code, body_message_count
            if message["type"] == "http.response.start":
                status_code = message["status"]
            elif message["type"] == "http.response.body":
                body_message_count += 1
                chunk = message.get("body", b"")
                if chunk:
                    body_chunks.append(chunk)

        await app(scope, receive, send)
        return status_code, body_chunks, body_message_count > 1

    def test_body_streams_in_multiple_chunks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_flow(monkeypatch, state=_state(), corpus=_corpus())
        _install_fake_model("alpha beta gamma delta [r_0001]")

        async def _run() -> tuple[int, list[bytes], bool]:
            return await asyncio.wait_for(self._drive_asgi(), timeout=10)

        status_code, body_chunks, streamed = asyncio.run(_run())

        assert status_code == 200
        assert streamed, "expected the ASGI body to arrive in multiple chunks"
        events = _parse_sse(b"".join(body_chunks).decode())
        names = [name for name, _ in events]
        assert names.count("token") > 1
        assert names[-1] == "done"


# ---------------------------------------------------------------------------
# Rate limit (Task 4.1 — kept)
# ---------------------------------------------------------------------------


class TestRateLimit:
    """The per-IP + global question limit returns 429 with Retry-After."""

    def test_over_limit_returns_429_with_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RL_QUESTIONS_PER_IP_HOUR", "2")
        monkeypatch.setenv("RL_GLOBAL_AI_CALLS_PER_HOUR", "2")
        get_settings.cache_clear()
        _stub_flow(monkeypatch, state=_state(), corpus=_corpus())
        _install_fake_model("ok [r_0001]")
        client = TestClient(app, raise_server_exceptions=False)

        last_status = 200
        last_response = None
        for _ in range(5):
            last_response = client.post(
                f"/api/chat/datasets/{_DS}",
                json={"question": "hi", "conversation_id": _CONV},
            )
            last_status = last_response.status_code
            if last_status == 429:
                break

        assert last_status == 429
        assert last_response is not None
        assert last_response.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert "Retry-After" in last_response.headers
        assert last_response.headers["Retry-After"].isdigit()
