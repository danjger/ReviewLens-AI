"""Unit tests for the LOCAL-ONLY realtime relay (:mod:`app.realtime_dev`).

These prove the dev relay's fan-out contract using Starlette's WebSocket test
transport (``TestClient.websocket_connect``) — no live network. The relay is a
compose-only convenience that makes the browser's realtime channel work under
local ``make e2e``; it is never deployed, so these are plain unit tests.

Covered:
* a connected client receives a frame posted to ``POST /broadcast``;
* a broadcast with no clients connected is a no-op 200;
* two clients both receive the same frame;
* ``GET /healthz`` returns 200.
"""

from __future__ import annotations

from app.realtime_dev import app
from fastapi.testclient import TestClient

_FRAME = {"type": "dataset.status.changed", "dataset_id": "d1", "status": "updated"}


def test_healthz_returns_ok() -> None:
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_broadcast_reaches_connected_client() -> None:
    client = TestClient(app)
    with client.websocket_connect("/realtime") as ws:
        resp = client.post("/broadcast", json=_FRAME)
        assert resp.status_code == 200
        assert resp.json() == {"delivered": 1}
        assert ws.receive_json() == _FRAME


def test_broadcast_with_no_clients_is_noop() -> None:
    client = TestClient(app)
    resp = client.post("/broadcast", json=_FRAME)
    assert resp.status_code == 200
    assert resp.json() == {"delivered": 0}


def test_broadcast_reaches_both_clients() -> None:
    client = TestClient(app)
    with (
        client.websocket_connect("/realtime") as ws_a,
        client.websocket_connect("/realtime") as ws_b,
    ):
        resp = client.post("/broadcast", json=_FRAME)
        assert resp.status_code == 200
        assert resp.json() == {"delivered": 2}
        assert ws_a.receive_json() == _FRAME
        assert ws_b.receive_json() == _FRAME
