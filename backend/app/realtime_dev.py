"""LOCAL / DEV ONLY WebSocket relay — NEVER DEPLOYED TO AWS.

THIS MODULE IS NOT A PRODUCTION CODE PATH. It exists solely so the local Docker
Compose stack (and therefore ``make e2e``) has a browser-reachable WebSocket the
SPA's :mod:`useRealtime <frontend>` hook can connect to, delivering live
``check.updated`` / ``dataset.status.changed`` frames without the AWS API
Gateway v2 WebSocket API (which LocalStack does not provide here).

In AWS the realtime path is unchanged: the browser connects to the deployed API
Gateway WebSocket URL baked into the SPA build as ``VITE_WS_URL``, and
:mod:`app.handlers.push` delivers via ``apigatewaymanagementapi.postToConnection``
against ``ws_api_endpoint``. This relay is never referenced by any CDK stack and
is never run as a Lambda command, so it has no effect on production.

This is the ONE place in the codebase allowed to hold in-process connection
state (a set of live sockets). tech.md requires stateless services whose
correctness never depends on process memory — that rule is about the production
services (API, chat, consumers). This dev relay is explicitly NOT a production
service: it is a compose-only convenience, clearly isolated in its own module
and its own ``realtime-dev`` container, so its in-memory connection set is the
documented dev exception, not a violation of the rule.

Transport:
* ``GET  /healthz``   — liveness for the compose healthcheck.
* ``WS   /realtime``  — the browser connects here (proxied by vite from
  ``ws://localhost:5173/realtime`` in local dev).
* ``POST /broadcast`` — the push consumer posts a frame here (configured via
  ``WS_LOCAL_RELAY_URL``); the relay fans it out to every connected browser.

No Lambda / API Gateway event shapes are imported here, and no user-supplied URL
is fetched, so the SSRF guard is not involved (the relay URL is an operator-set
internal compose address). Only opaque socket handles are held — no client IPs.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

app = FastAPI(title="ReviewLens realtime-dev relay (LOCAL ONLY)")

#: Live browser sockets. In-process state — the documented dev exception (see
#: the module docstring); this is never the production delivery path.
_clients: set[WebSocket] = set()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe for the compose healthcheck."""
    return {"status": "ok"}


@app.websocket("/realtime")
async def realtime(websocket: WebSocket) -> None:
    """Accept a browser socket and hold it open until it disconnects.

    The relay never sends anything on its own in response to client messages; it
    only pushes frames fanned out from :func:`broadcast`. We loop on
    ``receive_text`` purely to detect the disconnect, removing the socket from
    the registry in a ``finally`` so a dropped client is reaped.
    """
    await websocket.accept()
    _clients.add(websocket)
    logger.info("realtime-dev: client connected (%d total)", len(_clients))
    try:
        while True:
            # The browser does not send; this blocks until the socket closes.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(websocket)
        logger.info("realtime-dev: client disconnected (%d total)", len(_clients))


@app.post("/broadcast")
async def broadcast(frame: dict[str, Any]) -> dict[str, Any]:
    """Fan one frame out to every connected browser socket.

    ``frame`` is the already-built client frame the push handler would otherwise
    hand to ``postToConnection`` (``{"type": <event>, ...detail}``), so the
    browser receives the identical shape it does in AWS. A socket that raises on
    send is dropped. With no clients connected this is a no-op 200.
    """
    delivered = 0
    for socket in list(_clients):
        try:
            await socket.send_json(frame)
            delivered += 1
        except Exception:  # noqa: BLE001 - drop a dead socket, keep delivering
            _clients.discard(socket)
            logger.info("realtime-dev: dropped a dead socket during broadcast")
    return {"delivered": delivered}
