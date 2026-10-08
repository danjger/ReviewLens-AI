"""``$disconnect`` route handler for the WebSocket API (dataset-library 4.1).

The companion to :mod:`app.realtime.connect`: when API Gateway reports a socket
closed, delete its ``ws-connections`` row so the push consumer stops targeting
it (Requirement 6.1). Deleting an already-absent row is a no-op, so a duplicate
``$disconnect`` (or one that races the push consumer's stale-connection
cleanup) is harmless.

Like the connect handler, this is real-time glue for the managed WebSocket API
and is allowed to inspect the Lambda WebSocket event shape.
"""

from __future__ import annotations

import logging
from typing import Any

from app.realtime import connections

logger = logging.getLogger(__name__)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ANN401
    """Delete the closed connection's row and acknowledge.

    Always returns ``200`` — there is nothing a non-200 could usefully do on a
    socket that is already gone, and the TTL would reap the row regardless.
    """
    connection_id = event.get("requestContext", {}).get("connectionId")
    if not connection_id:
        logger.warning("$disconnect event without a connectionId; ignoring")
        return {"statusCode": 200}

    connections.remove_connection(connection_id)
    logger.info("ws disconnected: %s", connection_id)
    return {"statusCode": 200}
