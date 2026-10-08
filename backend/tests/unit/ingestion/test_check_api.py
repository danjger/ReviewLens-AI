"""Unit/route tests for the Check endpoints (dataset-ingestion task 4.3).

These exercise the three Check routes in :mod:`app.ingestion.api` through a
FastAPI ``TestClient`` with the Check Session store, the rate limiter, and the
SQS enqueue helper faked at the router's module boundary (the same style as the
check-handler unit tests). They stay fully offline and assert on the HTTP
contract from the design's "API endpoints" table and Requirements 1.1, 1.2,
1.4, 1.5 (shape the UI blocks on), 1.6, and 3.10.

Covered:

- ``POST /ingest/checks`` returns ``202`` with per-item initial states
  (pending / invalid / duplicate_in_batch), creates one session (origin "new"),
  and enqueues exactly one message per *pending* item (Req 1.1, 1.2, 1.4).
- an empty submission is ``422`` with the shared error envelope.
- a rate-limit hit is ``429`` with a numeric ``Retry-After`` header and the
  ``RATE_LIMIT_EXCEEDED`` code (Req 1.6); no session is created and nothing is
  enqueued.
- ``GET /ingest/checks/{id}`` returns the items with verdict/evidence and never
  exposes the ``#session`` header row; ``404`` for an expired/absent session.
- retry re-queues an ``error`` item and a timed-out (``done`` + timeout verdict)
  item, resets it to ``pending`` (Req 3.10); refuses a non-retryable item with
  ``422``; ``404`` for an unknown item/session; ``429`` when rate-limited.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.core.errors import RateLimitError
from app.ingestion import api as api_mod
from app.ingestion.check_session import CheckItem, CheckSession
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSessionStore:
    """In-memory stand-in for app.ingestion.check_session used by the router."""

    def __init__(self, session: CheckSession | None = None) -> None:
        self.session = session
        self.put_calls: list[dict[str, Any]] = []
        self.field_writes: list[dict[str, Any]] = []

    def put_session(
        self,
        check_id: str,
        origin: str,
        items: list[CheckItem],
        refresh_dataset_id: str | None = None,
    ) -> CheckSession:
        self.put_calls.append({"check_id": check_id, "origin": origin, "items": items})
        self.session = CheckSession(
            check_id=check_id,
            created_at="2024-01-01T00:00:00+00:00",
            ttl=1,
            origin=origin,  # type: ignore[arg-type]
            items={i.item_id: i for i in items},
        )
        return self.session

    def get_session(self, check_id: str) -> CheckSession | None:
        if self.session is not None and self.session.check_id == check_id:
            return self.session
        return None

    def set_item_fields(
        self,
        check_id: str,
        item_id: str,
        fields: dict[str, Any],
        *,
        expected_state: str | None = None,
    ) -> bool:
        self.field_writes.append(
            {"item_id": item_id, "fields": fields, "expected_state": expected_state}
        )
        if self.session is not None and item_id in self.session.items:
            item = self.session.items[item_id]
            if "state" in fields:
                item.state = fields["state"]  # type: ignore[assignment]
        return True


@pytest.fixture()
def wiring(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the store, limiter, and enqueue; return captured calls and the store."""
    store = _FakeSessionStore()
    enqueued: list[tuple[str, dict[str, Any]]] = []
    rate_calls: list[tuple[str, str | None, int]] = []

    monkeypatch.setattr(api_mod, "check_session", store)
    monkeypatch.setattr(
        api_mod,
        "enqueue",
        lambda queue_url, body, **kw: enqueued.append((queue_url, body)),
    )
    monkeypatch.setattr(
        api_mod,
        "check_rate_limit",
        lambda action, ip, limit, **kw: rate_calls.append((action, ip, limit)),
    )
    # No CloudFront header in tests → the limiter is called with ip=None.
    monkeypatch.setattr(api_mod, "get_client_ip", lambda request: None)
    return {"store": store, "enqueued": enqueued, "rate_calls": rate_calls}


@pytest.fixture()
def client(wiring: dict[str, Any]) -> TestClient:
    """A TestClient for an app that mounts only the ingestion router.

    Mirrors app.api wiring (router under /api + the shared error handlers) but
    without the origin-guard middleware, so tests focus on the Check contract.
    """
    from app.core.errors import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)
    api_mod.register_rate_limit_header(app)
    app.include_router(api_mod.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /ingest/checks
# ---------------------------------------------------------------------------


def test_create_check_returns_202_with_item_states(
    client: TestClient, wiring: dict[str, Any]
) -> None:
    resp = client.post(
        "/api/ingest/checks",
        json={"urls": ["https://a.example/reviews", "not a url", "https://a.example/reviews"]},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["check_id"]
    states = {i["item_id"]: i["state"] for i in body["items"]}
    assert states == {"u1": "pending", "u2": "invalid", "u3": "duplicate_in_batch"}
    # The invalid line carries a message for the UI.
    invalid = next(i for i in body["items"] if i["item_id"] == "u2")
    assert invalid["message"]


def test_create_check_creates_one_session_origin_new(
    client: TestClient, wiring: dict[str, Any]
) -> None:
    client.post("/api/ingest/checks", json={"urls": ["https://a.example/reviews"]})
    assert len(wiring["store"].put_calls) == 1
    assert wiring["store"].put_calls[0]["origin"] == "new"


def test_create_check_enqueues_only_pending_items(
    client: TestClient, wiring: dict[str, Any]
) -> None:
    client.post(
        "/api/ingest/checks",
        json={
            "urls": [
                "https://a.example/reviews",  # pending  -> enqueued
                "nonsense",  # invalid  -> not enqueued
                "https://a.example/reviews",  # dup       -> not enqueued
                "https://b.example/reviews",  # pending  -> enqueued
            ]
        },
    )
    enqueued = wiring["enqueued"]
    assert len(enqueued) == 2
    item_ids = sorted(body["item_id"] for _, body in enqueued)
    assert item_ids == ["u1", "u4"]
    # Message bodies are IDs only.
    for _, body in enqueued:
        assert set(body.keys()) == {"check_id", "item_id"}


def test_create_check_rate_limited_is_checked(client: TestClient, wiring: dict[str, Any]) -> None:
    client.post("/api/ingest/checks", json={"urls": ["https://a.example/reviews"]})
    assert wiring["rate_calls"] == [("checks", None, 30)]


def test_empty_submission_is_422(client: TestClient, wiring: dict[str, Any]) -> None:
    resp = client.post("/api/ingest/checks", json={"urls": ["   ", ""]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    # Nothing persisted or enqueued for an all-blank submission.
    assert wiring["store"].put_calls == []
    assert wiring["enqueued"] == []


def test_missing_urls_field_is_422(client: TestClient, wiring: dict[str, Any]) -> None:
    resp = client.post("/api/ingest/checks", json={})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Rate limiting (429 + Retry-After)
# ---------------------------------------------------------------------------


def test_create_check_429_sets_retry_after(
    client: TestClient, wiring: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _blocked(action: str, ip: str | None, limit: int, **kw: Any) -> None:
        raise RateLimitError("Rate limit exceeded. Retry after 42 seconds.")

    monkeypatch.setattr(api_mod, "check_rate_limit", _blocked)

    resp = client.post("/api/ingest/checks", json={"urls": ["https://a.example/reviews"]})
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert resp.headers.get("Retry-After") == "42"
    # No session created, nothing enqueued when the limit is hit.
    assert wiring["store"].put_calls == []
    assert wiring["enqueued"] == []


# ---------------------------------------------------------------------------
# GET /ingest/checks/{id}
# ---------------------------------------------------------------------------


def _session_with_items() -> CheckSession:
    done = CheckItem(
        item_id="u1",
        input="https://a.example/reviews",
        state="done",
        normalized="https://a.example/reviews",
        final_url="https://a.example/reviews",
        verdict={"verdict": "will_work", "reasons": ["24 reviews"], "evidence": {}},
    )
    pending = CheckItem(
        item_id="u2",
        input="https://b.example/reviews",
        state="pending",
        normalized="https://b.example/reviews",
    )
    return CheckSession(
        check_id="chk-1",
        created_at="2024-01-01T00:00:00+00:00",
        ttl=1,
        origin="new",
        items={"u1": done, "u2": pending},
    )


def test_get_check_returns_items_with_verdict(client: TestClient, wiring: dict[str, Any]) -> None:
    wiring["store"].session = _session_with_items()
    resp = client.get("/api/ingest/checks/chk-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["check_id"] == "chk-1"
    assert body["origin"] == "new"
    item_ids = {i["item_id"] for i in body["items"]}
    assert item_ids == {"u1", "u2"}
    u1 = next(i for i in body["items"] if i["item_id"] == "u1")
    assert u1["verdict"]["verdict"] == "will_work"


def test_get_check_never_exposes_session_header(client: TestClient, wiring: dict[str, Any]) -> None:
    wiring["store"].session = _session_with_items()
    body = client.get("/api/ingest/checks/chk-1").json()
    assert all(i["item_id"] != "#session" for i in body["items"])


def test_get_check_expired_is_404(client: TestClient, wiring: dict[str, Any]) -> None:
    wiring["store"].session = None
    resp = client.get("/api/ingest/checks/missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def _retry_session(state: str, verdict: dict[str, Any] | None) -> CheckSession:
    item = CheckItem(
        item_id="u1",
        input="https://a.example/reviews",
        state=state,  # type: ignore[arg-type]
        normalized="https://a.example/reviews",
        verdict=verdict,
    )
    return CheckSession(
        check_id="chk-1",
        created_at="2024-01-01T00:00:00+00:00",
        ttl=1,
        origin="new",
        items={"u1": item},
    )


def test_retry_error_item_requeues_and_resets_pending(
    client: TestClient, wiring: dict[str, Any]
) -> None:
    wiring["store"].session = _retry_session("error", None)
    resp = client.post("/api/ingest/checks/chk-1/items/u1/retry")
    assert resp.status_code == 202
    assert resp.json()["state"] == "pending"
    # Reset to pending and re-enqueued.
    assert wiring["store"].field_writes[-1]["fields"] == {"state": "pending"}
    assert wiring["enqueued"] == [(wiring_queue_url(), {"check_id": "chk-1", "item_id": "u1"})]


def test_retry_timed_out_item_requeues(client: TestClient, wiring: dict[str, Any]) -> None:
    timeout_verdict = {"verdict": "wont_work", "reasons": ["Page took too long to load"]}
    wiring["store"].session = _retry_session("done", timeout_verdict)
    resp = client.post("/api/ingest/checks/chk-1/items/u1/retry")
    assert resp.status_code == 202
    assert len(wiring["enqueued"]) == 1


def test_retry_non_retryable_done_is_422(client: TestClient, wiring: dict[str, Any]) -> None:
    # A genuine wont_work (404), not a timeout, is not retryable.
    wiring["store"].session = _retry_session(
        "done", {"verdict": "wont_work", "reasons": ["Status 404"]}
    )
    resp = client.post("/api/ingest/checks/chk-1/items/u1/retry")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert wiring["enqueued"] == []


def test_retry_unknown_item_is_404(client: TestClient, wiring: dict[str, Any]) -> None:
    wiring["store"].session = _retry_session("error", None)
    resp = client.post("/api/ingest/checks/chk-1/items/nope/retry")
    assert resp.status_code == 404


def test_retry_expired_session_is_404(client: TestClient, wiring: dict[str, Any]) -> None:
    wiring["store"].session = None
    resp = client.post("/api/ingest/checks/gone/items/u1/retry")
    assert resp.status_code == 404


def test_retry_rate_limited_is_429(
    client: TestClient, wiring: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    wiring["store"].session = _retry_session("error", None)

    def _blocked(action: str, ip: str | None, limit: int, **kw: Any) -> None:
        raise RateLimitError("Rate limit exceeded. Retry after 10 seconds.")

    monkeypatch.setattr(api_mod, "check_rate_limit", _blocked)

    resp = client.post("/api/ingest/checks/chk-1/items/u1/retry")
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "10"
    assert wiring["enqueued"] == []


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def wiring_queue_url() -> str:
    """The check queue URL the router reads from settings (empty by default)."""
    from app.core.config import get_settings

    return get_settings().check_queue_url
