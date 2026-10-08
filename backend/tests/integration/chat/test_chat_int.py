"""Integration tests for the streaming chat endpoint with the AI stub (guardrailed-chat 4.4).

Tasks 4.1–4.3's unit tests (``tests/unit/chat/test_service.py`` and
``tests/unit/chat/test_persist.py``) proved the request flow, SSE framing, the
availability/length guards, the Exchange shape, the HMAC signature, and the
save/no-save paths offline, with the DB (``_load_dataset_state``), the Corpus
loader, and ``load_recent_exchanges`` monkeypatched at the module boundary and
S3/DynamoDB on a moto backend.

This suite is task 4.4: it drives the **real** ``POST /api/chat/datasets/{id}``
endpoint against the backing services the design's "Integration tests (AI stub)"
bullet names — the Compose **PostgreSQL** database (the availability guard reads
a real ``datasets`` row and real ``dataset_versions`` rows) and the LocalStack
**S3** bucket (``corpus.load_corpus`` reads a seeded ``reviews/v{n}.json``; the
saved Exchange is written under ``datasets/{id}/chat/``) — with the AI stubbed
(no live model, no ``@pytest.mark.live_ai``). It covers:

- **stream** — token events then a terminal ``done`` event (Requirement 7.1/1.1);
- **saved object shape** — the Exchange persisted at the ``chat/{iso_ts}-{uuid}``
  key, including citation snippets, read back from LocalStack (Requirements 2.2,
  5.1);
- **409** for an archived dataset and for a dataset with no active version, read
  from the **real** ``Dataset`` row (Requirements 1.2, 1.3);
- **chat answering from v1 while v2 processes** — ``active_version=1`` with a v2
  row still ``requested``/processing; the chat answers from v1 and stays
  available (Requirement 1.1);
- **another conversation's Exchanges excluded from context** — a prior Exchange
  object for a *different* ``conversation_id`` is seeded under
  ``datasets/{id}/chat/`` and does not reach the assembled messages
  (Requirement 2.6);
- **429** — the per-IP/global question limit returns a pre-stream 429
  (design "Error Handling");
- **save-failure path** — an S3 put failure yields ``done`` with ``saved:false``
  and no event (Requirement 5.5);
- **mid-stream error** — a provider error after some tokens yields an SSE
  ``error`` event and nothing saved (design "Error Handling").

Running
-------
Run with ``make test-int`` under ``make up`` (needs LocalStack **and** the
Compose PostgreSQL). The module and each test skip cleanly when either is
unavailable, so the suite still *collects* without the stack. Each test uses its
own random dataset ids and cleans up its database rows and its ``datasets/{id}/``
S3 prefix (mirroring ``tests/integration/datasets/test_summary_int.py``). S3 keys
come only from :mod:`app.storage.keys`.

The AI is driven through the **real** instrumented :class:`AiClient` wrapping an
inline fake streaming client (``_FakeAnthropic``, the same shape the service unit
tests use): the chat path is streaming, and the fake lets a test assert tokens
deterministically and force a mid-stream failure without a recorded fixture. The
EventBridge publisher is spied so no event target has to be wired in LocalStack
(matching ``test_processing_pipeline_int.py``'s publisher spy).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from app.chat import service
from app.chat.service import app
from app.core import db as core_db
from app.core.ai import AiClient, reset_ai_client, set_ai_client
from app.core.config import get_settings
from app.db.models import Base, Dataset, DatasetStatus, SourceType
from app.storage import keys, s3
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"
_SIGNING_SECRET = "integration-chat-signing-secret"


# ---------------------------------------------------------------------------
# Availability probes (skip cleanly without the stack) — mirror test_summary_int
# ---------------------------------------------------------------------------


def _localstack_up() -> bool:
    try:
        resp = httpx.get(f"{_ENDPOINT}/_localstack/health", timeout=2.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _database_reachable() -> bool:
    try:
        with core_db.get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any failure means skip
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _require_stack() -> None:
    if not _localstack_up():
        pytest.skip("LocalStack not reachable; run under `make test-int`")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point AWS clients at LocalStack, disable the origin guard, set the secret.

    Follows ``test_summary_int.py``'s ``_env``: set the LocalStack env, clear
    ``ORIGIN_VERIFY_SECRET`` so the origin guard runs in local-dev bypass mode
    (the TestClient sends no CloudFront header), set a deterministic chat signing
    secret so the saved/returned Exchange signatures are stable, clear the
    memoised settings so the middleware/clients re-read them, and reset the
    cached S3 client so it rebinds to LocalStack. Also keep the per-IP/global
    question limits high so the normal cases never hit a 429 (the 429 case sets
    them low itself). Restore caches on teardown.
    """
    monkeypatch.setenv("AWS_ENDPOINT_URL", _ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", _S3_BUCKET)
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", "")
    monkeypatch.setenv("CHAT_SIGNING_SECRET", _SIGNING_SECRET)
    monkeypatch.setenv("RL_QUESTIONS_PER_IP_HOUR", "1000")
    monkeypatch.setenv("RL_GLOBAL_AI_CALLS_PER_HOUR", "1000")
    get_settings.cache_clear()
    s3.reset_client()
    from app.events import publisher

    publisher.reset_client()
    try:
        yield
    finally:
        get_settings.cache_clear()
        s3.reset_client()
        publisher.reset_client()
        reset_ai_client()


@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    """Ensure the DB schema exists; skip when no PostgreSQL is reachable."""
    get_settings.cache_clear()
    core_db.reset_engine()
    if get_settings().is_aws or not _database_reachable():
        pytest.skip("PostgreSQL not reachable; run under `make test-int` with the stack up")
    engine = core_db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(engine)
    yield
    core_db.reset_engine()


@pytest.fixture()
def published() -> Iterator[list[dict[str, Any]]]:
    """Spy on the chat service's ``publish_event`` so no real bus is needed.

    The ``chat.exchange.saved`` publish on a successful save is captured here
    instead of being sent to LocalStack EventBridge, matching the publisher-spy
    pattern used by ``test_processing_pipeline_int.py`` (wiring an event
    rule/target in LocalStack is platform-foundation's concern, not this test's).
    Patches the name where ``app.chat.service`` imported it.
    """
    captured: list[dict[str, Any]] = []
    original = service.publish_event

    def _fake(detail_type: str, detail: dict[str, Any]) -> None:
        captured.append({"detail_type": detail_type, "detail": detail})

    service.publish_event = _fake  # type: ignore[assignment]
    try:
        yield captured
    finally:
        service.publish_event = original  # type: ignore[assignment]


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """A FastAPI test client with the Corpus cache cleared around each test.

    The Corpus loader keeps a warm ``(dataset_id, version)`` cache; clearing it
    before and after each test keeps the random-id datasets independent so a
    seeded ``reviews/v{n}.json`` is always read fresh from LocalStack.
    """
    from app.chat import corpus as corpus_mod

    corpus_mod.reset_corpus_cache()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        corpus_mod.reset_corpus_cache()


@pytest.fixture()
def dataset_factory() -> Iterator[Callable[..., str]]:
    """Insert a dataset (+ version rows) and optionally seed its reviews in S3.

    Returns a maker that inserts one dataset with the given availability fields
    and seeds its ``dataset_versions`` rows (one per version up to
    ``data_version``). Every version strictly below ``data_version`` is completed
    (``outcome='updated'``). The top version ``data_version`` is completed only
    when it equals ``active_version`` — so passing ``active_version < data_version``
    seeds the "v{active} active while v{data_version} is still processing"
    scenario (a top row with ``requested_at`` set but ``completed_at`` /
    ``outcome`` NULL). When ``reviews`` is given, writes
    ``reviews/v{active_version}.json`` to LocalStack via :mod:`app.storage.keys`
    so ``corpus.load_corpus`` can read it. Every DB row and the whole
    ``datasets/{id}/`` S3 prefix are removed at teardown so each test owns its
    own ids (testing convention).
    """
    created: list[str] = []

    def _make(
        *,
        name: str = "Chat dataset",
        source_type: SourceType = SourceType.URL,
        original_url: str | None = "https://example.com/product/reviews",
        platform: str | None = "g2",
        status: DatasetStatus = DatasetStatus.UPDATED,
        data_version: int = 1,
        active_version: int | None = 1,
        archived: bool = False,
        entity: dict[str, Any] | None = None,
        reviews: list[dict[str, Any]] | None = None,
    ) -> str:
        ds_id = str(uuid.uuid4())
        url = original_url if source_type == SourceType.URL else None
        with core_db.session_scope() as session:
            session.add(
                Dataset(
                    id=ds_id,
                    name=name,
                    source_type=source_type,
                    original_url=url,
                    final_url=url,
                    normalized_url=url,
                    normalized_final_url=url,
                    platform=platform if url else None,
                    status=status,
                    status_detail={"events": [{"status": status.value, "message": "seed event"}]},
                    data_version=data_version,
                    active_version=active_version,
                )
            )
            if archived:
                session.execute(
                    text("UPDATE datasets SET archived_at = now() WHERE id = CAST(:id AS uuid)"),
                    {"id": ds_id},
                )
            for v in range(1, data_version + 1):
                # Completed for any version at/below the active one; the top
                # version is left pending (processing) when it is above active.
                done = active_version is not None and v <= active_version
                session.execute(
                    text(
                        "INSERT INTO dataset_versions "
                        "(dataset_id, version, trigger, requested_at, completed_at, "
                        " review_count, extraction_method, outcome) "
                        "VALUES (CAST(:id AS uuid), :v, :trigger, now(), "
                        "        CASE WHEN :done THEN now() ELSE NULL END, "
                        "        CASE WHEN :done THEN :rc ELSE NULL END, "
                        "        CASE WHEN :done THEN 'ai' ELSE NULL END, "
                        "        CASE WHEN :done THEN 'updated' ELSE NULL END)"
                    ),
                    {
                        "id": ds_id,
                        "v": v,
                        "trigger": "initial" if v == 1 else "manual_refresh",
                        "done": done,
                        "rc": (len(reviews) if reviews is not None else 10),
                    },
                )
        created.append(ds_id)

        if reviews is not None and active_version is not None:
            key = keys.dataset_reviews(ds_id, active_version)
            doc = {
                "dataset_id": ds_id,
                "version": active_version,
                "entity": entity or {"name": "Acme CRM", "category": "software"},
                "reviews": reviews,
            }
            s3.put_bytes(
                key,
                json.dumps(doc).encode("utf-8"),
                content_type="application/json",
            )
        return ds_id

    try:
        yield _make
    finally:
        s3_client = s3._get_s3_client()
        with core_db.session_scope() as session:
            for ds_id in created:
                session.execute(
                    text("DELETE FROM dataset_versions WHERE dataset_id = CAST(:id AS uuid)"),
                    {"id": ds_id},
                )
                session.execute(
                    text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
                )
        for ds_id in created:
            listed = s3_client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=keys.dataset_prefix(ds_id))
            for obj in listed.get("Contents", []):
                obj_key = obj.get("Key")
                if obj_key:
                    s3_client.delete_object(Bucket=_S3_BUCKET, Key=obj_key)


# ---------------------------------------------------------------------------
# Review / Exchange seed helpers
# ---------------------------------------------------------------------------


def _review(
    rid: str,
    *,
    text: str = "Great support, quick to respond.",
    rating: int | None = 5,
    date: str = "2026-01-01",
) -> dict[str, Any]:
    """A review item shaped like an entry of ``reviews/v{n}.json``."""
    return {
        "id": rid,
        "text": text,
        "rating": rating,
        "date": date,
        "author": "Alice",
        "title": None,
        "sentiment": "positive",
        "source_page": 1,
    }


def _seed_prior_exchange(
    ds_id: str,
    *,
    conversation_id: str,
    data_version: int,
    question: str,
    answer: str,
    asked_at: str,
) -> None:
    """Write one prior Exchange object under ``datasets/{id}/chat/`` for a test.

    Mirrors the Exchange shape the save step writes (design "Data Models"), with
    the key built from :mod:`app.storage.keys` so its ISO-timestamp prefix sorts
    chronologically. Used by the "another conversation excluded" case to plant a
    *different* conversation's Exchange that must not reach the assembled context.
    """
    exchange_id = str(uuid.uuid4())
    key = keys.dataset_chat_exchange(ds_id, asked_at, exchange_id)
    body = {
        "id": exchange_id,
        "dataset_id": ds_id,
        "data_version": data_version,
        "conversation_id": conversation_id,
        "asked_at": asked_at,
        "answered_at": asked_at,
        "question": question,
        "answer": answer,
        "citations": [],
        "dropped_citations": 0,
        "citation_snippets": {},
        "scope": "in_scope",
        "scope_category": None,
        "precheck": None,
        "model": "claude-test",
        "prompt_version": "system_v1",
        "usage": {"input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0},
        "saved": True,
    }
    s3.put_bytes(key, json.dumps(body).encode("utf-8"), content_type="application/json")


# ---------------------------------------------------------------------------
# Fake streaming model (mirrors tests/unit/chat/test_service.py)
# ---------------------------------------------------------------------------


class _FakeStream:
    """An iterable of SDK-shaped stream events built from text word chunks.

    Optionally raises after *raise_after* text chunks to simulate a provider
    error mid-stream (design "Error Handling").
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
        self.captured: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        assert kwargs.get("stream") is True  # noqa: S101 - the chat uses streaming
        self.captured.append(kwargs)
        return _FakeStream(self._text, raise_after=self._raise_after)


class _FakeAnthropic:
    def __init__(self, text: str, *, raise_after: int | None = None) -> None:
        self.messages = _FakeMessages(text, raise_after=raise_after)


def _install_fake_model(text: str, *, raise_after: int | None = None) -> _FakeMessages:
    """Point the process-wide instrumented client at a fake streaming model.

    Returns the ``_FakeMessages`` so a test can read the ``messages`` the service
    actually assembled and sent (used to assert context exclusion).
    """
    fake = _FakeAnthropic(text, raise_after=raise_after)
    set_ai_client(AiClient(client=fake))
    return fake.messages


# ---------------------------------------------------------------------------
# SSE / request helpers
# ---------------------------------------------------------------------------


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE stream body into a list of (event name, data) tuples."""
    events: list[tuple[str, dict[str, Any]]] = []
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


def _stream(
    client: TestClient, ds_id: str, body: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """POST a chat request and return the parsed SSE events (expects a 200 stream)."""
    with client.stream("POST", f"/api/chat/datasets/{ds_id}", json=body) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    return _parse_sse(text)


# ===========================================================================
# Stream + saved object shape (Requirements 1.1, 2.2, 5.1, 7.1)
# ===========================================================================


def test_streams_tokens_then_saves_exchange_with_snippets(
    client: TestClient,
    dataset_factory: Callable[..., str],
    published: list[dict[str, Any]],
) -> None:
    """A full request streams tokens, then saves the Exchange with snippets to S3.

    Drives the real endpoint against a seeded ``reviews/v1.json`` in LocalStack
    and a real ``datasets`` row: token events reconstruct the answer, the
    terminal ``done`` carries the Exchange with the valid citation and its saved
    snippet, and the object is persisted at the ``chat/{asked_at}-{id}.json`` key
    with ``saved=true``. _Validates: Requirements 1.1, 2.2, 5.1, 7.1._
    """
    ds_id = dataset_factory(
        reviews=[_review("r_0001", text="Support was great."), _review("r_0002", rating=2)]
    )
    _install_fake_model("The main praise is support [r_0001].")

    events = _stream(client, ds_id, {"question": "top praise?", "conversation_id": "conv-a"})
    names = [n for n, _ in events]

    # Tokens stream, then a terminal done (Requirement 7.1).
    assert names.count("token") > 1
    assert names[-1] == "done"
    tokens = "".join(d["text"] for n, d in events if n == "token")
    assert tokens == "The main praise is support [r_0001]."

    exchange = events[-1][1]
    assert exchange["dataset_id"] == ds_id
    assert exchange["data_version"] == 1
    assert exchange["conversation_id"] == "conv-a"
    assert exchange["question"] == "top praise?"
    assert exchange["citations"] == ["r_0001"]
    # The snippet is copied from the seeded Corpus review (Requirement 2.2).
    assert exchange["citation_snippets"]["r_0001"]["text"] == "Support was great."
    assert exchange["scope"] == "in_scope"
    assert exchange["saved"] is True
    assert exchange["usage"]["output_tokens"] == 7
    assert exchange["usage"]["cache_read_tokens"] == 100

    # The object is persisted in LocalStack at the ISO-ts key (Requirement 5.1).
    key = keys.dataset_chat_exchange(ds_id, exchange["asked_at"], exchange["id"])
    stored = json.loads(s3.get_text(key))
    assert stored["question"] == "top praise?"
    assert stored["citations"] == ["r_0001"]
    assert stored["citation_snippets"]["r_0001"]["text"] == "Support was great."
    assert stored["saved"] is True

    # A chat.exchange.saved event was published with IDs only (Requirement 5.7).
    assert len(published) == 1
    assert published[0]["detail_type"] == "chat.exchange.saved"
    assert published[0]["detail"]["dataset_id"] == ds_id
    assert published[0]["detail"]["exchange_id"] == exchange["id"]
    assert "answer" not in published[0]["detail"]


def test_phantom_citation_is_dropped_against_seeded_corpus(
    client: TestClient, dataset_factory: Callable[..., str], published: list[dict[str, Any]]
) -> None:
    """A citation to an ID not in the seeded Corpus is removed (Requirement 2.2/2.5)."""
    ds_id = dataset_factory(reviews=[_review("r_0001")])
    _install_fake_model("Praise [r_0001] and a phantom [r_9999].")

    events = _stream(client, ds_id, {"question": "praise?", "conversation_id": "conv-a"})
    exchange = events[-1][1]

    assert exchange["citations"] == ["r_0001"]
    assert exchange["dropped_citations"] == 1
    assert "r_9999" not in exchange["citation_snippets"]


# ===========================================================================
# 409 CHAT_UNAVAILABLE from the real Dataset row (Requirements 1.2, 1.3)
# ===========================================================================


def test_409_for_archived_dataset(client: TestClient, dataset_factory: Callable[..., str]) -> None:
    """An archived dataset answers 409, read from the real archived_at (Req 1.3)."""
    ds_id = dataset_factory(archived=True, reviews=[_review("r_0001")])
    _install_fake_model("unused")

    resp = client.post(f"/api/chat/datasets/{ds_id}", json={"question": "hi"})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CHAT_UNAVAILABLE"


def test_409_for_dataset_with_no_active_version(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """A still-processing first version (no active version) answers 409 (Req 1.2)."""
    ds_id = dataset_factory(
        status=DatasetStatus.PROCESSING, data_version=1, active_version=None, reviews=None
    )
    _install_fake_model("unused")

    resp = client.post(f"/api/chat/datasets/{ds_id}", json={"question": "hi"})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CHAT_UNAVAILABLE"


# ===========================================================================
# Chat answers from v1 while v2 processes (Requirement 1.1)
# ===========================================================================


def test_answers_from_v1_while_v2_processes(
    client: TestClient, dataset_factory: Callable[..., str], published: list[dict[str, Any]]
) -> None:
    """With ``active_version=1`` and a v2 row still processing, chat answers from v1.

    Seeds a real dataset at ``data_version=2`` / ``active_version=1`` — a v1 row
    completed and a v2 row with ``requested_at`` set but ``completed_at`` /
    ``outcome`` NULL — plus a ``reviews/v1.json`` Corpus. The availability guard
    (reading the real row, not a stub) keeps the chat available, and the answer
    is grounded in v1 with ``data_version=1`` on the saved Exchange
    (Requirement 1.1). _Validates: Requirement 1.1._
    """
    ds_id = dataset_factory(
        status=DatasetStatus.PROCESSING,  # a refresh is in flight
        data_version=2,
        active_version=1,
        reviews=[_review("r_0001", text="Reliable and fast.")],
    )
    _install_fake_model("Reviewers call it reliable [r_0001].")

    events = _stream(client, ds_id, {"question": "is it reliable?", "conversation_id": "conv-a"})

    assert events[-1][0] == "done"
    exchange = events[-1][1]
    # Answered against the active version (v1), not the processing v2.
    assert exchange["data_version"] == 1
    assert exchange["citations"] == ["r_0001"]
    assert exchange["citation_snippets"]["r_0001"]["text"] == "Reliable and fast."
    assert exchange["saved"] is True


# ===========================================================================
# Another conversation's Exchanges excluded from context (Requirement 2.6)
# ===========================================================================


def test_other_conversation_exchanges_excluded_from_context(
    client: TestClient, dataset_factory: Callable[..., str], published: list[dict[str, Any]]
) -> None:
    """A different conversation's prior Exchange is never sent as context.

    Seeds two prior Exchanges under ``datasets/{id}/chat/``: one for the asking
    tab's conversation (``conv-a``) and one for a *different* conversation
    (``conv-other``). The next request from ``conv-a`` must assemble messages
    that include only ``conv-a``'s prior turn — ``conv-other``'s question/answer
    text must not appear in anything sent to the model (Requirement 2.6).

    Asserts on the **real** messages the service sent (captured off the fake
    client), so this exercises ``load_recent_exchanges`` reading S3 and
    ``assemble_messages`` filtering, end to end. _Validates: Requirement 2.6._
    """
    ds_id = dataset_factory(reviews=[_review("r_0001")])
    _seed_prior_exchange(
        ds_id,
        conversation_id="conv-a",
        data_version=1,
        question="MINE earlier question",
        answer="MINE earlier answer [r_0001].",
        asked_at="2026-01-01T00:00:00+00:00",
    )
    _seed_prior_exchange(
        ds_id,
        conversation_id="conv-other",
        data_version=1,
        question="OTHER secret question",
        answer="OTHER secret answer.",
        asked_at="2026-01-01T00:00:01+00:00",
    )
    fake_messages = _install_fake_model("Follow-up grounded answer [r_0001].")

    events = _stream(client, ds_id, {"question": "follow up?", "conversation_id": "conv-a"})
    assert events[-1][0] == "done"

    # Inspect exactly what the service sent to the model.
    assert len(fake_messages.captured) == 1
    sent = fake_messages.captured[0]
    serialized = json.dumps(sent["messages"])

    # The asking conversation's prior turn IS included as context...
    assert "MINE earlier question" in serialized
    assert "MINE earlier answer" in serialized
    # ...but the other conversation's Exchange is NOT.
    assert "OTHER secret question" not in serialized
    assert "OTHER secret answer" not in serialized


# ===========================================================================
# 429 rate limit (design "Error Handling")
# ===========================================================================


def test_429_when_question_limit_exceeded(
    client: TestClient, dataset_factory: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-IP/global question limit returns a pre-stream 429 with Retry-After.

    Lowers the limits for this test only (the ``_env`` fixture otherwise keeps
    them high) and asks repeatedly until the limiter trips. The 429 is an
    ordinary HTTP error (not a mid-stream SSE event). _Validates: design
    "Error Handling"._
    """
    monkeypatch.setenv("RL_QUESTIONS_PER_IP_HOUR", "2")
    monkeypatch.setenv("RL_GLOBAL_AI_CALLS_PER_HOUR", "2")
    get_settings.cache_clear()
    ds_id = dataset_factory(reviews=[_review("r_0001")])
    _install_fake_model("ok [r_0001]")

    last = None
    for _ in range(6):
        last = client.post(
            f"/api/chat/datasets/{ds_id}", json={"question": "hi", "conversation_id": "conv-a"}
        )
        if last.status_code == 429:
            break

    assert last is not None
    assert last.status_code == 429
    assert last.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert "Retry-After" in last.headers
    assert last.headers["Retry-After"].isdigit()


# ===========================================================================
# Save-failure path (Requirement 5.5)
# ===========================================================================


def test_save_failure_yields_saved_false_and_no_event(
    client: TestClient,
    dataset_factory: Callable[..., str],
    published: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the S3 put fails, ``done`` carries ``saved:false`` and no event fires.

    Forces the Exchange write to fail (after the signature is attached) and
    asserts the answer is still delivered with ``saved=false``, nothing is
    published, and no Exchange object is left in S3. _Validates: Requirement 5.5._
    """
    ds_id = dataset_factory(reviews=[_review("r_0001")])
    _install_fake_model("Support is praised [r_0001].")

    real_put = service.s3.put_bytes

    def _boom(key: str, body: bytes, *, content_type: str) -> None:
        if "/chat/" in key:
            raise RuntimeError("s3 unavailable")
        real_put(key, body, content_type=content_type)

    monkeypatch.setattr(service.s3, "put_bytes", _boom)

    events = _stream(client, ds_id, {"question": "praise?", "conversation_id": "conv-a"})
    exchange = events[-1][1]

    assert events[-1][0] == "done"
    assert exchange["saved"] is False
    assert exchange["answer"] == "Support is praised [r_0001]."
    # The signature is present so the UI can replay to the retry-save endpoint.
    assert isinstance(exchange.get("signature"), str) and exchange["signature"]
    # No event published, and nothing written under the chat prefix.
    assert published == []
    assert s3.list_keys(keys.dataset_chat_prefix(ds_id)) == []


# ===========================================================================
# Mid-stream error (design "Error Handling")
# ===========================================================================


def test_mid_stream_error_emits_error_event_and_saves_nothing(
    client: TestClient,
    dataset_factory: Callable[..., str],
    published: list[dict[str, Any]],
) -> None:
    """A provider error mid-stream emits an SSE ``error`` event; nothing is saved.

    The fake raises after one token; the endpoint surfaces an ``error`` event
    (no ``done``), publishes nothing, and leaves the chat prefix empty.
    _Validates: design "Error Handling"._
    """
    ds_id = dataset_factory(reviews=[_review("r_0001")])
    _install_fake_model("partial answer here", raise_after=1)

    events = _stream(client, ds_id, {"question": "what?", "conversation_id": "conv-a"})
    names = [n for n, _ in events]

    assert "token" in names
    assert names[-1] == "error"
    assert "done" not in names
    assert events[-1][1]["code"] == "ANSWER_INTERRUPTED"
    # Nothing persisted and nothing published on a mid-stream failure.
    assert published == []
    assert s3.list_keys(keys.dataset_chat_prefix(ds_id)) == []
