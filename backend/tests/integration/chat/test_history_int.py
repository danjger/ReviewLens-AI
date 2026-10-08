"""Integration tests: history / suggestions APIs and the refresh scenario (guardrailed-chat 5.4).

Tasks 5.1–5.3's unit tests proved the pure timeline merge, the retry-save HMAC
gate, and the suggestion templates offline. This suite is task 5.4: it drives
the **real** read endpoints the shared Q&A log and starter chips use against the
backing services the design's "Integration tests (AI stub)" bullet names — the
Compose **PostgreSQL** database (``datasets`` + ``dataset_versions`` rows, read
for ``active_version`` and the refresh markers) and the LocalStack **S3** bucket
(seeded chat Exchange objects under ``datasets/{id}/chat/`` and the
``reviews/v{n}.json`` Corpus) — with the AI stubbed (no live model). It covers:

1. **history order** — several Exchange objects from mixed conversations plus
   ``dataset_versions`` rows are returned oldest-first, merged with refresh
   markers (Requirements 5.2, 9.1);
2. **paging** — more than one page of items is paged backward with the ``before``
   cursor (page size 20), with no overlap and no gaps across a page boundary
   that straddles a refresh marker (Requirement 5.3);
3. **version labels / stale** — with ``active_version=2`` the v1 Exchanges carry
   ``is_stale=true`` and the v2 ones ``is_stale=false``, and the completed v2
   marker is present (Requirements 5.4, 9.4, 9.1);
4. **the refresh scenario (headline)** — a dataset active on v1 with a v1
   Exchange is promoted to v2 (a completed v2 ``dataset_versions`` row,
   ``active_version=2``, a seeded ``reviews/v2.json``); then (a) ``GET history``
   shows the v1 Exchange ``is_stale=true`` with a completed v2 marker, and (b) a
   chat request on the **same** ``conversation_id`` assembles messages that
   **exclude** the v1 Exchange (Requirement 9.6 — only current-version Exchanges
   are context); and
5. **suggestions** (light) — a dataset whose ``metrics`` carry themes yields
   theme-based questions, and a dataset with no metrics yields the general
   fallbacks (Requirement 1.4, touched lightly as the task's focus is 5.2/5.3/5.4
   and 9.1/9.4/9.6).

Running
-------
Run with ``make test-int`` under ``make up`` (needs LocalStack **and** the
Compose PostgreSQL). The module and each test skip cleanly when either is
unavailable, so the suite still *collects* without the stack. Each test uses its
own random dataset ids and cleans up its database rows and its ``datasets/{id}/``
S3 prefix (mirroring ``tests/integration/chat/test_chat_int.py`` and
``tests/integration/datasets/test_summary_int.py``). S3 keys come only from
:mod:`app.storage.keys`.

The history and suggestions endpoints are ordinary **API-service** routes
(``app.api:app``); the refresh scenario's chat step drives the **chat-service**
app (``app.chat.service:app``) streaming endpoint with the same inline fake
streaming client used by ``test_chat_int.py``, so the assembled context can be
read off the fake and asserted. The EventBridge publisher is spied so no event
target has to be wired in LocalStack.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from app.api import app as api_app
from app.chat import service
from app.chat.service import app as chat_app
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
_SIGNING_SECRET = "integration-history-signing-secret"


# ---------------------------------------------------------------------------
# Availability probes (skip cleanly without the stack) — mirror test_chat_int
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

    Mirrors ``test_chat_int.py``'s ``_env``: set the LocalStack env, clear
    ``ORIGIN_VERIFY_SECRET`` so the origin guard runs in local-dev bypass mode
    (the TestClient sends no CloudFront header), set a deterministic chat signing
    secret, clear the memoised settings so the middleware/clients re-read them,
    and reset the cached S3 and publisher clients so they rebind to LocalStack.
    Keep the per-IP/global question limits high so the refresh scenario's chat
    call never trips a 429. Restore caches on teardown.
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

    Only the refresh scenario's chat step publishes (``chat.exchange.saved`` on a
    successful save); capturing it here matches the publisher-spy pattern in
    ``test_chat_int.py`` / ``test_processing_pipeline_int.py``.
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
def api_client() -> Iterator[TestClient]:
    """A test client for the **API service** app (history + suggestions routes)."""
    yield TestClient(api_app, raise_server_exceptions=False)


@pytest.fixture()
def chat_client() -> Iterator[TestClient]:
    """A test client for the **chat service** app, Corpus cache cleared each test.

    The Corpus loader keeps a warm ``(dataset_id, version)`` cache; clearing it
    around each test keeps the random-id datasets independent so a seeded
    ``reviews/v{n}.json`` is always read fresh from LocalStack.
    """
    from app.chat import corpus as corpus_mod

    corpus_mod.reset_corpus_cache()
    try:
        yield TestClient(chat_app, raise_server_exceptions=False)
    finally:
        corpus_mod.reset_corpus_cache()


@pytest.fixture()
def dataset_factory() -> Iterator[Callable[..., str]]:
    """Insert a dataset row and clean up its DB rows + S3 prefix at teardown.

    Returns a maker that inserts one ``datasets`` row with the given
    availability/metrics fields. Unlike ``test_chat_int.py``'s factory it does
    **not** seed version rows or reviews — this suite builds those explicitly per
    test via :func:`_seed_version` / :func:`_seed_reviews` so each test controls
    the exact timeline (markers, counts, outcomes) it asserts on. Every DB row
    (dataset + any versions) and the whole ``datasets/{id}/`` S3 prefix are
    removed at teardown so each test owns its own ids (testing convention).
    """
    created: list[str] = []

    def _make(
        *,
        name: str = "History dataset",
        source_type: SourceType = SourceType.URL,
        original_url: str | None = "https://example.com/product/reviews",
        platform: str | None = "g2",
        status: DatasetStatus = DatasetStatus.UPDATED,
        data_version: int = 1,
        active_version: int | None = 1,
        archived: bool = False,
        metrics: dict[str, Any] | None = None,
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
                    status_detail={"events": [{"status": status.value, "message": "seed"}]},
                    data_version=data_version,
                    active_version=active_version,
                    metrics=metrics,
                )
            )
            if archived:
                session.execute(
                    text("UPDATE datasets SET archived_at = now() WHERE id = CAST(:id AS uuid)"),
                    {"id": ds_id},
                )
        created.append(ds_id)
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
# DB / S3 seed helpers (version rows, reviews, Exchange objects)
# ---------------------------------------------------------------------------


def _seed_version(
    ds_id: str,
    version: int,
    *,
    trigger: str = "manual_refresh",
    requested_at: str,
    completed_at: str | None = None,
    review_count: int | None = None,
    outcome: str | None = None,
) -> None:
    """Insert one ``dataset_versions`` row with explicit timestamps and outcome.

    Timestamps are passed as ISO-8601 strings and cast to ``timestamptz`` so a
    test can place markers deterministically on the timeline (unlike the chat
    factory's ``now()`` rows). ``outcome=None`` / ``completed_at=None`` seeds a
    still-in-flight (pending) version; ``outcome='updated'`` with a
    ``completed_at`` seeds a completed one; ``outcome='failed'`` a failed one.
    """
    with core_db.session_scope() as session:
        session.execute(
            text(
                "INSERT INTO dataset_versions "
                "(dataset_id, version, trigger, requested_at, completed_at, "
                " review_count, extraction_method, outcome) "
                "VALUES (CAST(:id AS uuid), :v, :trigger, "
                "        CAST(:requested_at AS timestamptz), "
                "        CAST(:completed_at AS timestamptz), "
                "        :rc, :method, :outcome)"
            ),
            {
                "id": ds_id,
                "v": version,
                "trigger": trigger,
                "requested_at": requested_at,
                "completed_at": completed_at,
                "rc": review_count,
                "method": "ai" if outcome == "updated" else None,
                "outcome": outcome,
            },
        )


def _seed_reviews(
    ds_id: str,
    version: int,
    reviews: list[dict[str, Any]],
    *,
    entity: dict[str, Any] | None = None,
) -> None:
    """Write a ``reviews/v{n}.json`` Corpus for *version* to LocalStack S3."""
    key = keys.dataset_reviews(ds_id, version)
    doc = {
        "dataset_id": ds_id,
        "version": version,
        "entity": entity or {"name": "Acme CRM", "category": "software"},
        "reviews": reviews,
    }
    s3.put_bytes(key, json.dumps(doc).encode("utf-8"), content_type="application/json")


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


def _seed_exchange(
    ds_id: str,
    *,
    conversation_id: str,
    data_version: int,
    asked_at: str,
    question: str = "What do reviewers say?",
    answer: str = "They praise support.",
) -> str:
    """Write one Exchange object under ``datasets/{id}/chat/`` and return its id.

    Mirrors the Exchange shape the chat save step writes (design "Data Models"),
    keyed by its ``asked_at`` ISO timestamp via :mod:`app.storage.keys` so the
    chronological prefix listing (and thus the history timeline) is stable.
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
    return exchange_id


# ---------------------------------------------------------------------------
# Fake streaming model (mirrors tests/integration/chat/test_chat_int.py)
# ---------------------------------------------------------------------------


class _FakeStream:
    """An iterable of SDK-shaped stream events built from text word chunks."""

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
        self.captured: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        assert kwargs.get("stream") is True  # noqa: S101 - the chat uses streaming
        self.captured.append(kwargs)
        return _FakeStream(self._text)


class _FakeAnthropic:
    def __init__(self, text: str) -> None:
        self.messages = _FakeMessages(text)


def _install_fake_model(text: str) -> _FakeMessages:
    """Point the process-wide instrumented client at a fake streaming model.

    Returns the ``_FakeMessages`` so a test can read the ``messages`` the service
    assembled and sent (used to assert the stale Exchange is excluded).
    """
    fake = _FakeAnthropic(text)
    set_ai_client(AiClient(client=fake))
    return fake.messages


# ---------------------------------------------------------------------------
# SSE / request helpers (mirror test_chat_int)
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


def _history(
    client: TestClient, ds_id: str, *, before: str | None = None, limit: int | None = None
) -> dict[str, Any]:
    """GET one page of the shared history timeline and return the JSON body."""
    params: dict[str, Any] = {}
    if before is not None:
        params["before"] = before
    if limit is not None:
        params["limit"] = limit
    resp = client.get(f"/api/datasets/{ds_id}/chat/history", params=params)
    assert resp.status_code == 200
    return resp.json()


# ===========================================================================
# 1. History order — Exchanges + markers merged oldest-first (Req 5.2, 9.1)
# ===========================================================================


def test_history_returns_merged_timeline_oldest_first(
    api_client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """Mixed-conversation Exchanges and version rows merge into one ordered log.

    Seeds a dataset active on v2 with a v1 row (initial, no marker) and a
    completed v2 row (a marker), plus three Exchanges from two conversations at
    distinct timestamps straddling the v2 completion. ``GET history`` returns
    everything oldest-first (Requirement 5.2), with exactly one refresh marker
    (version 2, the only version > 1; Requirement 9.1), placed at the v2
    completion time between the v1 and v2 Exchanges. _Validates: Requirements
    5.2, 9.1._
    """
    ds_id = dataset_factory(data_version=2, active_version=2)
    _seed_version(
        ds_id,
        1,
        trigger="add",
        requested_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:05:00+00:00",
        review_count=100,
        outcome="updated",
    )
    _seed_version(
        ds_id,
        2,
        trigger="manual_refresh",
        requested_at="2026-01-02T00:00:00+00:00",
        completed_at="2026-01-02T00:05:00+00:00",
        review_count=120,
        outcome="updated",
    )
    # Two v1 Exchanges (conv-a, conv-b) before the v2 completion, one v2 after.
    _seed_exchange(
        ds_id,
        conversation_id="conv-a",
        data_version=1,
        asked_at="2026-01-01T10:00:00+00:00",
        question="Q1 on v1",
    )
    _seed_exchange(
        ds_id,
        conversation_id="conv-b",
        data_version=1,
        asked_at="2026-01-01T11:00:00+00:00",
        question="Q2 on v1",
    )
    _seed_exchange(
        ds_id,
        conversation_id="conv-a",
        data_version=2,
        asked_at="2026-01-02T10:00:00+00:00",
        question="Q3 on v2",
    )

    body = _history(api_client, ds_id)
    items = body["items"]

    # Everything is on one page, oldest-first, with no older pages.
    assert body["has_more"] is False
    assert body["next_before"] is None
    kinds = [it["type"] for it in items]
    assert kinds == ["exchange", "exchange", "refresh_marker", "exchange"]

    # The questions are in time order; the shared log includes both conversations.
    exchange_questions = [it["question"] for it in items if it["type"] == "exchange"]
    assert exchange_questions == ["Q1 on v1", "Q2 on v1", "Q3 on v2"]

    # Exactly one marker, for version 2, completed, with before/after counts.
    markers = [it for it in items if it["type"] == "refresh_marker"]
    assert len(markers) == 1
    marker = markers[0]
    assert marker["version"] == 2
    assert marker["state"] == "completed"
    assert marker["review_count"] == 120
    assert marker["previous_review_count"] == 100


# ===========================================================================
# 2. Paging — backward by the shared cursor across a marker (Req 5.3)
# ===========================================================================


def test_history_pages_backward_without_overlap_or_gaps(
    api_client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """More than a page of items pages backward with ``before``; no overlap/gaps.

    Seeds 25 Exchanges plus a completed v2 marker (26 timeline items) so the
    newest page (size 20) and the older page (6 items) straddle the marker. The
    two pages, concatenated oldest-first, reconstruct the full timeline exactly
    once each — proving the shared cursor pages Exchanges and the marker together
    with no repeats and no gaps at the boundary. _Validates: Requirement 5.3._
    """
    ds_id = dataset_factory(data_version=2, active_version=2)
    _seed_version(
        ds_id,
        1,
        trigger="add",
        requested_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:05:00+00:00",
        review_count=100,
        outcome="updated",
    )
    # The v2 marker sits in the middle of the day's Exchanges (at 12:30).
    _seed_version(
        ds_id,
        2,
        trigger="manual_refresh",
        requested_at="2026-01-05T12:00:00+00:00",
        completed_at="2026-01-05T12:30:00+00:00",
        review_count=120,
        outcome="updated",
    )

    # 25 Exchanges at one-minute spacing across 12:00..12:24 (some before the
    # marker at 12:30, the rest would be after if later — here all before, but
    # the marker at 12:30 is newer than all of them, so it is the newest item).
    def _ts(minute: int) -> str:
        return f"2026-01-05T12:{minute:02d}:00+00:00"

    expected_questions: list[str] = []
    for minute in range(25):
        q = f"Q{minute:02d}"
        expected_questions.append(q)
        _seed_exchange(
            ds_id, conversation_id="conv-a", data_version=2, asked_at=_ts(minute), question=q
        )

    # Newest page (no cursor): the 20 newest items. The marker (12:30) is newest,
    # so this page is the marker + the 19 newest Exchanges (12:06..12:24).
    page1 = _history(api_client, ds_id)
    assert len(page1["items"]) == 20
    assert page1["has_more"] is True
    assert page1["next_before"] is not None

    # Older page via the cursor: the remaining 6 items (12:00..12:05).
    page2 = _history(api_client, ds_id, before=page1["next_before"])
    assert len(page2["items"]) == 6
    assert page2["has_more"] is False
    assert page2["next_before"] is None

    # Reconstruct the full timeline oldest-first: page2 (older) then page1.
    merged = page2["items"] + page1["items"]
    assert len(merged) == 26  # 25 Exchanges + 1 marker, each exactly once.

    # Exactly one marker, and it appears once (no overlap, no gap at the seam).
    marker_count = sum(1 for it in merged if it["type"] == "refresh_marker")
    assert marker_count == 1

    # Every Exchange appears exactly once, in time order.
    merged_questions = [it["question"] for it in merged if it["type"] == "exchange"]
    assert merged_questions == expected_questions

    # No id is repeated across the two pages (no overlap).
    ids = [it["id"] for it in merged if it["type"] == "exchange"]
    assert len(ids) == len(set(ids))


# ===========================================================================
# 3. Version labels / stale flags (Req 5.4, 9.4, 9.1)
# ===========================================================================


def test_history_flags_old_version_exchanges_as_stale(
    api_client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """With ``active_version=2``, v1 Exchanges are stale and v2 ones are fresh.

    Seeds Exchanges on both v1 and v2 with the dataset active on v2 and a
    completed v2 marker. The history flags exactly the v1 Exchanges
    ``is_stale=true`` (their ``data_version`` is older than ``active_version``;
    Requirement 9.4) and the v2 one ``is_stale=false``, and the v2 marker is
    present (Requirements 5.4, 9.1). _Validates: Requirements 5.4, 9.4, 9.1._
    """
    ds_id = dataset_factory(data_version=2, active_version=2)
    _seed_version(
        ds_id,
        1,
        trigger="add",
        requested_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:05:00+00:00",
        review_count=100,
        outcome="updated",
    )
    _seed_version(
        ds_id,
        2,
        trigger="manual_refresh",
        requested_at="2026-01-02T00:00:00+00:00",
        completed_at="2026-01-02T00:05:00+00:00",
        review_count=120,
        outcome="updated",
    )
    _seed_exchange(
        ds_id,
        conversation_id="conv-a",
        data_version=1,
        asked_at="2026-01-01T10:00:00+00:00",
        question="old on v1",
    )
    _seed_exchange(
        ds_id,
        conversation_id="conv-a",
        data_version=2,
        asked_at="2026-01-02T10:00:00+00:00",
        question="new on v2",
    )

    items = _history(api_client, ds_id)["items"]
    by_question = {it["question"]: it for it in items if it["type"] == "exchange"}

    assert by_question["old on v1"]["is_stale"] is True
    assert by_question["old on v1"]["data_version"] == 1
    assert by_question["new on v2"]["is_stale"] is False
    assert by_question["new on v2"]["data_version"] == 2

    markers = [it for it in items if it["type"] == "refresh_marker"]
    assert [m["version"] for m in markers] == [2]
    assert markers[0]["state"] == "completed"


# ===========================================================================
# 4. The refresh scenario (headline): ask on v1, refresh to v2 (Req 9.4, 9.6, 9.1)
# ===========================================================================


def test_refresh_scenario_marks_v1_stale_and_excludes_it_from_next_chat(
    api_client: TestClient,
    chat_client: TestClient,
    dataset_factory: Callable[..., str],
    published: list[dict[str, Any]],
) -> None:
    """Ask on v1, refresh to v2: v1 Exchange is stale, v2 marker shows, v1 excluded.

    Seeds a dataset active on v1 with a seeded ``reviews/v1.json`` and a v1
    Exchange (the "asked on v1" turn) on ``conv-a``. Then promotes the dataset to
    v2: inserts a completed v2 ``dataset_versions`` row, sets
    ``active_version=2``/``data_version=2``, and seeds ``reviews/v2.json``.

    Part (a): ``GET history`` now flags the v1 Exchange ``is_stale=true`` and
    shows a completed v2 refresh marker (Requirements 9.4, 9.1).

    Part (b): a new chat request on the **same** ``conversation_id`` (``conv-a``)
    assembles messages that **exclude** the v1 Exchange — only current-version
    (v2) Exchanges are context (Requirement 9.6). Exclusion is asserted both on
    the real messages the fake model received and on the new Exchange's
    ``data_version`` (2, i.e. answered against v2). _Validates: Requirements 9.4,
    9.6, 9.1._
    """
    ds_id = dataset_factory(data_version=1, active_version=1)
    _seed_version(
        ds_id,
        1,
        trigger="add",
        requested_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:05:00+00:00",
        review_count=100,
        outcome="updated",
    )
    _seed_reviews(ds_id, 1, [_review("r_0001", text="V1 says support is slow.")])
    _seed_exchange(
        ds_id,
        conversation_id="conv-a",
        data_version=1,
        asked_at="2026-01-01T10:00:00+00:00",
        question="V1 ONLY earlier question",
        answer="V1 ONLY earlier answer [r_0001].",
    )

    # --- Promote to v2: a completed refresh. ---
    _seed_version(
        ds_id,
        2,
        trigger="manual_refresh",
        requested_at="2026-01-02T00:00:00+00:00",
        completed_at="2026-01-02T00:05:00+00:00",
        review_count=120,
        outcome="updated",
    )
    _seed_reviews(ds_id, 2, [_review("r_0001", text="V2 says support improved.")])
    with core_db.session_scope() as session:
        session.execute(
            text(
                "UPDATE datasets SET active_version = 2, data_version = 2 "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": ds_id},
        )

    # --- Part (a): history shows the v1 Exchange stale + a completed v2 marker. ---
    items = _history(api_client, ds_id)["items"]
    v1_exchange = next(
        it
        for it in items
        if it["type"] == "exchange" and it["question"] == "V1 ONLY earlier question"
    )
    assert v1_exchange["is_stale"] is True
    assert v1_exchange["data_version"] == 1

    markers = [it for it in items if it["type"] == "refresh_marker"]
    assert [m["version"] for m in markers] == [2]
    assert markers[0]["state"] == "completed"
    assert markers[0]["review_count"] == 120
    assert markers[0]["previous_review_count"] == 100

    # --- Part (b): the next chat on conv-a excludes the v1 Exchange. ---
    fake_messages = _install_fake_model("Support has improved [r_0001].")
    events = _stream(
        chat_client, ds_id, {"question": "how is support now?", "conversation_id": "conv-a"}
    )
    assert events[-1][0] == "done"
    new_exchange = events[-1][1]
    # The new answer is grounded in the active version (v2), not the stale v1.
    assert new_exchange["data_version"] == 2

    # The messages the model actually received must not carry the v1 turn.
    assert len(fake_messages.captured) == 1
    serialized = json.dumps(fake_messages.captured[0]["messages"])
    assert "V1 ONLY earlier question" not in serialized
    assert "V1 ONLY earlier answer" not in serialized


# ===========================================================================
# 5. Suggestions (light) — theme-based vs. fallback (Req 1.4)
# ===========================================================================


def test_suggestions_from_themes_then_fallback_without_metrics(
    api_client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """A dataset with themes yields theme questions; one without yields fallbacks.

    Lightly touches the suggestions endpoint alongside the task's history focus:
    a dataset whose ``Dataset.metrics`` carries themes returns the template
    question for each theme label (Requirement 1.4), and a dataset with no
    metrics returns the general fallback questions. _Validates: Requirement 1.4._
    """
    # Upload-type (no URL) so the two datasets don't collide on the unique
    # partial index over ``normalized_url`` for URL datasets.
    themed = dataset_factory(
        source_type=SourceType.UPLOAD,
        original_url=None,
        metrics={
            "themes": [
                {"label": "customer support", "mentions": 40, "lean": "negative"},
                {"label": "pricing", "mentions": 25, "lean": "mixed"},
                {"label": "onboarding", "mentions": 10, "lean": "positive"},
            ]
        },
    )
    resp = api_client.get(f"/api/datasets/{themed}/chat/suggestions")
    assert resp.status_code == 200
    suggestions = resp.json()["suggestions"]
    assert "What do reviewers say about customer support?" in suggestions
    assert "What do reviewers say about pricing?" in suggestions
    assert 3 <= len(suggestions) <= 4

    plain = dataset_factory(source_type=SourceType.UPLOAD, original_url=None, metrics=None)
    resp2 = api_client.get(f"/api/datasets/{plain}/chat/suggestions")
    assert resp2.status_code == 200
    fallbacks = resp2.json()["suggestions"]
    # The general fallbacks fit any review set (no theme templates present).
    assert "What are the most common complaints?" in fallbacks
    assert all("What do reviewers say about" not in q for q in fallbacks)
    assert 3 <= len(fallbacks) <= 4
