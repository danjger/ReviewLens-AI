"""Route tests for the Add endpoint (dataset-ingestion task 7.3).

These exercise ``POST /ingest/checks/{check_id}/add`` in :mod:`app.ingestion.api`
through a FastAPI ``TestClient`` with :func:`app.ingestion.service.add_items`
faked at the router's module boundary (the same style as
``test_check_api.py``). They stay fully offline and assert on the HTTP contract
from the design's "API endpoints" table and Requirements 5.4, 6.5, 6.6.

The endpoint is a thin controller: ``add_items`` owns every per-item decision,
so these tests prove the controller (a) maps the request body to
``AddRequestItem``\\ s (including ``confirm_limited``), (b) always returns
``200`` with ``{results: [...]}`` serialized via ``AddResult.to_dict()`` —
including an ``expired`` session — and (c) rejects an empty ``items`` list with
the shared ``422`` error envelope.

Covered:

- every outcome in the design's vocabulary is passed straight through:
  ``created`` / ``refreshed`` / ``restored_and_refreshed`` /
  ``already_refreshing`` / ``refused_wont_work`` / ``needs_confirmation`` /
  ``expired`` (Requirements 5.4, 6.5, 6.6).
- ``dataset_id`` and ``message`` round-trip in each result row.
- ``confirm_limited`` is forwarded to ``add_items`` per item.
- an expired/absent session still returns ``200`` (not ``404``) with every item
  ``expired``.
- an empty ``items`` list is ``422`` with the ``VALIDATION_ERROR`` envelope and
  ``add_items`` is never called.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.ingestion import api as api_mod
from app.ingestion.service import AddRequestItem, AddResult
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeService:
    """Stand-in for app.ingestion.service used by the router.

    Records every ``add_items`` call and replays a canned list of results, so
    the test controls the outcomes the controller must pass through.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results: list[AddResult] = []

    def add_items(self, check_id: str, items: list[AddRequestItem]) -> list[AddResult]:
        self.calls.append({"check_id": check_id, "items": items})
        return self.results


@pytest.fixture()
def fake_service(monkeypatch: pytest.MonkeyPatch) -> _FakeService:
    """Fake ``service.add_items`` at the router boundary; return the recorder."""
    svc = _FakeService()
    monkeypatch.setattr(api_mod, "service", svc)
    return svc


@pytest.fixture()
def client(fake_service: _FakeService) -> TestClient:
    """A TestClient for an app that mounts only the ingestion router.

    Mirrors app.api wiring (router under /api + the shared error handlers)
    without the origin-guard middleware, so tests focus on the Add contract.
    """
    from app.core.errors import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)
    api_mod.register_rate_limit_header(app)
    app.include_router(api_mod.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Outcome pass-through
# ---------------------------------------------------------------------------


def test_add_returns_200_with_results_passthrough(
    client: TestClient, fake_service: _FakeService
) -> None:
    """Every outcome and its dataset_id/message round-trip in the response."""
    fake_service.results = [
        AddResult("u1", "created", dataset_id="ds-1"),
        AddResult("u2", "refreshed", dataset_id="ds-2"),
        AddResult("u3", "restored_and_refreshed", dataset_id="ds-3"),
        AddResult("u4", "already_refreshing", dataset_id="ds-4"),
        AddResult("u5", "refused_wont_work", message="This page can't be read."),
        AddResult("u6", "needs_confirmation", message="Confirm to add it."),
    ]

    resp = client.post(
        "/api/ingest/checks/chk-1/add",
        json={
            "items": [
                {"item_id": "u1"},
                {"item_id": "u2"},
                {"item_id": "u3"},
                {"item_id": "u4"},
                {"item_id": "u5"},
                {"item_id": "u6"},
            ]
        },
    )

    assert resp.status_code == 200
    results = resp.json()["results"]
    by_id = {r["item_id"]: r for r in results}
    assert by_id["u1"] == {
        "item_id": "u1",
        "outcome": "created",
        "dataset_id": "ds-1",
        "message": None,
    }
    assert by_id["u2"]["outcome"] == "refreshed"
    assert by_id["u3"]["outcome"] == "restored_and_refreshed"
    assert by_id["u4"]["outcome"] == "already_refreshing"
    assert by_id["u5"] == {
        "item_id": "u5",
        "outcome": "refused_wont_work",
        "dataset_id": None,
        "message": "This page can't be read.",
    }
    assert by_id["u6"]["outcome"] == "needs_confirmation"


def test_add_forwards_check_id_and_items(client: TestClient, fake_service: _FakeService) -> None:
    """The controller passes the path check_id and body items to add_items."""
    fake_service.results = [AddResult("u1", "created", dataset_id="ds-1")]

    client.post("/api/ingest/checks/chk-xyz/add", json={"items": [{"item_id": "u1"}]})

    assert len(fake_service.calls) == 1
    call = fake_service.calls[0]
    assert call["check_id"] == "chk-xyz"
    assert call["items"] == [AddRequestItem(item_id="u1", confirm_limited=False)]


def test_add_forwards_confirm_limited(client: TestClient, fake_service: _FakeService) -> None:
    """confirm_limited is forwarded per item (default False when omitted)."""
    fake_service.results = [
        AddResult("u1", "created", dataset_id="ds-1"),
        AddResult("u2", "created", dataset_id="ds-2"),
    ]

    client.post(
        "/api/ingest/checks/chk-1/add",
        json={"items": [{"item_id": "u1", "confirm_limited": True}, {"item_id": "u2"}]},
    )

    sent = fake_service.calls[0]["items"]
    assert sent == [
        AddRequestItem(item_id="u1", confirm_limited=True),
        AddRequestItem(item_id="u2", confirm_limited=False),
    ]


# ---------------------------------------------------------------------------
# Expired session (still 200, not 404)
# ---------------------------------------------------------------------------


def test_add_expired_session_returns_200_with_expired(
    client: TestClient, fake_service: _FakeService
) -> None:
    """An expired/absent session yields outcome 'expired' per item, not a 404."""
    fake_service.results = [AddResult("u1", "expired"), AddResult("u2", "expired")]

    resp = client.post(
        "/api/ingest/checks/gone/add",
        json={"items": [{"item_id": "u1"}, {"item_id": "u2"}]},
    )

    assert resp.status_code == 200
    outcomes = [r["outcome"] for r in resp.json()["results"]]
    assert outcomes == ["expired", "expired"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_add_empty_items_is_422(client: TestClient, fake_service: _FakeService) -> None:
    """An empty items list is a client error; add_items is never called."""
    resp = client.post("/api/ingest/checks/chk-1/add", json={"items": []})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []


def test_add_missing_items_field_is_422(client: TestClient, fake_service: _FakeService) -> None:
    resp = client.post("/api/ingest/checks/chk-1/add", json={})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fake_service.calls == []
