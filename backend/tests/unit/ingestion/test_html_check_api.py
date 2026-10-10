"""Unit/route tests for ``POST /ingest/html-checks`` (dataset-ingestion task 19.1).

These exercise the single new HTML-upload endpoint in :mod:`app.ingestion.api`
through a FastAPI ``TestClient`` with the Check Session store, the rate limiter,
the SQS enqueue helper, and the S3 staged-object check faked at the router's
module boundary (the same style as ``test_check_api.py``). They stay fully
offline and assert on the HTTP contract from the design's "API endpoints" table
and Requirements 8.1, 8.3, 8.7.

Covered (task 19.1):

- happy path (``upload_id`` only) → ``202`` ``{check_id, item_id, state:"pending"}``,
  one ``origin="new"`` session whose single item carries ``upload_id`` and null
  ``source_url``/``normalized``, and exactly **one** ``check-queue`` message
  (IDs only).
- a supplied, well-formed ``source_url`` is normalized onto the item and used as
  its ``final_url`` (so the handler's duplicate lookup can match).
- missing staged object → ``422`` (and no session / nothing enqueued).
- malformed ``source_url`` → ``422`` (and no session / nothing enqueued), even
  though the staged object exists.
- a rate-limit hit on the shared ``checks`` counters → ``429`` with a numeric
  ``Retry-After`` header and the ``RATE_LIMIT_EXCEEDED`` code; no session, no
  enqueue.
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

    def __init__(self) -> None:
        self.session: CheckSession | None = None
        self.put_calls: list[dict[str, Any]] = []

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


@pytest.fixture()
def wiring(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the store, limiter, enqueue, and the S3 staged-object check."""
    store = _FakeSessionStore()
    enqueued: list[tuple[str, dict[str, Any]]] = []
    rate_calls: list[tuple[str, str | None, int]] = []
    # By default the staged object exists; individual tests flip this.
    object_exists = {"value": True}

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
    monkeypatch.setattr(api_mod, "get_client_ip", lambda request: None)
    # Fake ``s3.object_exists``. The router calls ``s3.object_exists(...)`` on
    # the shared storage module, so patching it there is what the route reads.
    from app.storage import s3 as s3_mod

    monkeypatch.setattr(s3_mod, "object_exists", lambda key: object_exists["value"])
    return {
        "store": store,
        "enqueued": enqueued,
        "rate_calls": rate_calls,
        "object_exists": object_exists,
    }


@pytest.fixture()
def client(wiring: dict[str, Any]) -> TestClient:
    """A TestClient for an app that mounts only the ingestion router."""
    from app.core.errors import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)
    api_mod.register_rate_limit_header(app)
    app.include_router(api_mod.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_html_check_happy_path_enqueues_one_message(
    client: TestClient, wiring: dict[str, Any]
) -> None:
    resp = client.post("/api/ingest/html-checks", json={"upload_id": "up-1"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["check_id"]
    assert body["item_id"] == "u1"
    assert body["state"] == "pending"

    # Exactly one origin="new" session was created.
    assert len(wiring["store"].put_calls) == 1
    put = wiring["store"].put_calls[0]
    assert put["origin"] == "new"
    item = put["items"][0]
    assert item.upload_id == "up-1"
    # No source URL supplied → no normalized / final URL (no URL dedupe).
    assert item.source_url is None
    assert item.normalized is None
    assert item.final_url is None

    # Exactly one check-queue message, IDs only.
    assert len(wiring["enqueued"]) == 1
    _, msg = wiring["enqueued"][0]
    assert set(msg.keys()) == {"check_id", "item_id"}
    assert msg["item_id"] == "u1"
    assert msg["check_id"] == body["check_id"]


def test_html_check_uses_shared_checks_rate_limit(
    client: TestClient, wiring: dict[str, Any]
) -> None:
    client.post("/api/ingest/html-checks", json={"upload_id": "up-1"})
    # Rate-limited on the SHARED ``checks`` action (not a separate counter).
    assert wiring["rate_calls"] == [("checks", None, 30)]


def test_html_check_with_source_url_normalizes_onto_item(
    client: TestClient, wiring: dict[str, Any]
) -> None:
    resp = client.post(
        "/api/ingest/html-checks",
        json={"upload_id": "up-2", "source_url": "HTTPS://WWW.Example.com/Reviews/"},
    )
    assert resp.status_code == 202
    item = wiring["store"].put_calls[0]["items"][0]
    # The source URL is kept as-is for provenance and used as final_url, and its
    # normalized form is stored for the duplicate lookup (Requirement 8.12).
    assert item.source_url == "HTTPS://WWW.Example.com/Reviews/"
    assert item.final_url == "HTTPS://WWW.Example.com/Reviews/"
    assert item.normalized == "https://example.com/Reviews"


def test_html_check_blank_source_url_is_treated_as_absent(
    client: TestClient, wiring: dict[str, Any]
) -> None:
    resp = client.post(
        "/api/ingest/html-checks",
        json={"upload_id": "up-3", "source_url": "   "},
    )
    assert resp.status_code == 202
    item = wiring["store"].put_calls[0]["items"][0]
    assert item.source_url is None
    assert item.normalized is None


# ---------------------------------------------------------------------------
# 422 paths
# ---------------------------------------------------------------------------


def test_html_check_missing_staged_object_is_422(
    client: TestClient, wiring: dict[str, Any]
) -> None:
    wiring["object_exists"]["value"] = False
    resp = client.post("/api/ingest/html-checks", json={"upload_id": "gone"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    # Nothing persisted or enqueued.
    assert wiring["store"].put_calls == []
    assert wiring["enqueued"] == []


def test_html_check_malformed_source_url_is_422(client: TestClient, wiring: dict[str, Any]) -> None:
    # The staged object exists, but the source_url is not a valid http(s) URL.
    resp = client.post(
        "/api/ingest/html-checks",
        json={"upload_id": "up-4", "source_url": "not a url"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert wiring["store"].put_calls == []
    assert wiring["enqueued"] == []


@pytest.mark.parametrize("bad_url", ["ftp://example.com/x", "javascript:alert(1)", "example.com"])
def test_html_check_rejects_non_http_source_urls(
    client: TestClient, wiring: dict[str, Any], bad_url: str
) -> None:
    resp = client.post(
        "/api/ingest/html-checks",
        json={"upload_id": "up-5", "source_url": bad_url},
    )
    assert resp.status_code == 422
    assert wiring["enqueued"] == []


def test_html_check_missing_upload_id_is_422(client: TestClient, wiring: dict[str, Any]) -> None:
    resp = client.post("/api/ingest/html-checks", json={})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 429 after the shared rate limit
# ---------------------------------------------------------------------------


def test_html_check_429_sets_retry_after(
    client: TestClient, wiring: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _blocked(action: str, ip: str | None, limit: int, **kw: Any) -> None:
        raise RateLimitError("Rate limit exceeded. Retry after 37 seconds.")

    monkeypatch.setattr(api_mod, "check_rate_limit", _blocked)

    resp = client.post("/api/ingest/html-checks", json={"upload_id": "up-6"})
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert resp.headers.get("Retry-After") == "37"
    # No session created, nothing enqueued when the limit is hit.
    assert wiring["store"].put_calls == []
    assert wiring["enqueued"] == []
