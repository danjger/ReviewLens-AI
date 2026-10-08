"""Push queue handler (dataset-library task 4.2).

Broadcasts domain events to every connected WebSocket client. The consumer
runtime (:mod:`app.consumer`) routes the ``push`` queue here and reads
``PUSH_QUEUE_URL``; this module supplies the business logic.

Flow (design "Realtime components"):

    EventBridge rule → SQS push-queue → this handler → postToConnection → browser

An EventBridge rule in the RealtimeStack forwards the two broadcast event types
to the push queue, so each message body is the EventBridge envelope
``{"detail-type": ..., "detail": {...}, ...}``. The handler turns that into a
small client frame ``{"type": <event>, ...detail}`` and posts it to every
connection ID in the ``ws-connections`` table, fanning the sends out
concurrently (adding push-consumer instances raises throughput — design).

Both event types are delivered to **every** connection; the browser decides
whether a given frame is relevant (a Library tab applies ``dataset.status.changed``;
a New Dataset panel applies ``check.updated`` only for the check it is showing).
The app has no sign-in, so there is no per-connection filtering here
(Requirements 6.2, 6.5).

Stale-connection cleanup: a ``postToConnection`` to a browser that has gone away
returns ``GoneException`` (HTTP 410); the handler deletes that connection's row
(Requirement 6.5). Any other send error is logged and skipped — one unreachable
client never blocks delivery to the rest, and a push failure never affects
processing (design "Error Handling").

Engineering rules honoured: stateless (the only cached state is boto3 client
handles); same code in both compute modes (no Lambda event shapes imported
here — the SQS adapter lives in :mod:`app.consumer`, the WebSocket glue in
:mod:`app.realtime`); idempotent (a repeated event re-sends a status the browser
already has, which the cache reducer collapses — design Correctness Property 4);
every outbound AWS client honours ``aws_endpoint_url`` for LocalStack; no raw
client IP is stored or logged (a connection ID is an opaque token).
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, cast

import boto3
from botocore.exceptions import ClientError

from app.consumer import MessageMeta
from app.core.config import get_settings
from app.realtime import connections

if TYPE_CHECKING:
    from mypy_boto3_apigatewaymanagementapi import ApiGatewayManagementApiClient

logger = logging.getLogger(__name__)

#: Event detail-types this handler broadcasts. Both are delivered to every
#: connection; the browser decides relevance. Kept in sync with the EventBridge
#: rule in the RealtimeStack and with the publishers in app.db.status and
#: app.handlers.check.
BROADCAST_DETAIL_TYPES = ("dataset.status.changed", "check.updated")

#: Maximum concurrent ``postToConnection`` calls per message. Caps the thread
#: fan-out so one large connection set cannot exhaust the runtime; well above a
#: realistic small-team connection count.
_MAX_FANOUT_WORKERS = 20

_mgmt_client: ApiGatewayManagementApiClient | None = None


def _get_mgmt_client() -> ApiGatewayManagementApiClient:
    """Return a shared API Gateway Management API client.

    The client is pointed at ``Settings.ws_api_endpoint`` (the WebSocket API's
    callback URL from the RealtimeStack), honouring ``aws_endpoint_url`` so
    LocalStack and integration tests work. Memoised per process; a connection
    pool, so the module stays stateless.
    """
    global _mgmt_client
    if _mgmt_client is None:
        settings = get_settings()
        endpoint_url = settings.aws_endpoint_url or settings.ws_api_endpoint
        _mgmt_client = cast(
            "ApiGatewayManagementApiClient",
            boto3.client("apigatewaymanagementapi", endpoint_url=endpoint_url),
        )
    return _mgmt_client


def reset_client() -> None:
    """Clear the cached management-API client (tests change settings/endpoints)."""
    global _mgmt_client
    _mgmt_client = None


class PushHandler:
    """Broadcast domain events to every connected WebSocket client."""

    queue: str = "push"

    def handle(self, body: dict[str, Any], meta: MessageMeta) -> None:
        """Fan one event out to every connection; clean up the stale ones.

        ``body`` is the EventBridge envelope delivered to the push queue. A body
        whose detail-type is not one of :data:`BROADCAST_DETAIL_TYPES` is
        dropped (returning normally so SQS deletes it) — the queue should only
        carry those, but the handler never fails on an unexpected shape.
        """
        detail_type = body.get("detail-type")
        if detail_type not in BROADCAST_DETAIL_TYPES:
            logger.warning(
                "push message %s has unexpected detail-type %r; dropping",
                meta.message_id,
                detail_type,
            )
            return

        detail = body.get("detail") or {}
        frame = self._frame(detail_type, detail)
        payload = json.dumps(frame, default=str).encode("utf-8")

        connection_ids = connections.list_connection_ids()
        if not connection_ids:
            logger.debug("no ws connections; nothing to push for %s", detail_type)
            return

        self._broadcast(connection_ids, payload)

    def _frame(self, detail_type: str, detail: dict[str, Any]) -> dict[str, Any]:
        """Build the client frame ``{"type": <event>, ...detail}``.

        The browser's event bus switches on ``type``; the rest of the detail is
        the row/check patch it applies. ``type`` always wins over any ``type``
        key that happened to be in the detail.
        """
        return {**detail, "type": detail_type}

    def _broadcast(self, connection_ids: list[str], payload: bytes) -> None:
        """Post the payload to every connection concurrently.

        Sends run on a bounded thread pool so a large connection set is fanned
        out in parallel rather than serially (design: "fanning out
        postToConnection calls concurrently"). Each send is independent: a
        failure to one connection never blocks the others.
        """
        workers = min(_MAX_FANOUT_WORKERS, len(connection_ids))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # list() forces every future to complete before the pool closes.
            list(pool.map(lambda cid: self._post_one(cid, payload), connection_ids))

    def _post_one(self, connection_id: str, payload: bytes) -> None:
        """Post to a single connection, reaping it on ``GoneException`` (410).

        A ``GoneException`` means the browser has disconnected without a
        ``$disconnect`` reaching us; delete its row so later broadcasts skip it
        (Requirement 6.5). Any other error is logged and swallowed so one bad
        connection never fails the batch (a push failure must not affect
        processing).
        """
        client = _get_mgmt_client()
        try:
            client.post_to_connection(ConnectionId=connection_id, Data=payload)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("GoneException", "410"):
                logger.info("ws connection %s is gone; cleaning up", connection_id)
                connections.remove_connection(connection_id)
            else:
                logger.warning(
                    "postToConnection failed for %s: %s", connection_id, code, exc_info=True
                )
        except Exception:  # noqa: BLE001 - never let one send fail the batch
            logger.warning("postToConnection crashed for %s", connection_id, exc_info=True)


handler = PushHandler()
