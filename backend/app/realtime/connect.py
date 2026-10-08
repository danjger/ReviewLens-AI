"""``$connect`` route handler for the WebSocket API (dataset-library 4.1).

This is one of the two tiny Lambda functions that are glue for the managed
WebSocket API (the design: they "stay as Lambda functions in both compute
modes, because they are glue for the managed WebSocket API rather than
application compute"). It is therefore allowed to inspect the Lambda WebSocket
event shape — that confinement to real-time glue mirrors the rule that only
:mod:`app.consumer` inspects SQS event shapes.

On connect it records ``{connection_id, connected_at, ttl}`` in the
``ws-connections`` table (Requirement 6.1) and returns ``200`` so API Gateway
accepts the socket. The WebSocket API has no credentials; abuse is bounded by
the stage's throttling limits set in the RealtimeStack.
"""

from __future__ import annotations

import logging
from typing import Any

from app.realtime import connections

logger = logging.getLogger(__name__)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ANN401
    """Store the new connection ID and accept the socket.

    A missing connection ID (a malformed event) is rejected with ``400`` rather
    than raising, so API Gateway never retries a request that can never succeed.
    """
    connection_id = event.get("requestContext", {}).get("connectionId")
    if not connection_id:
        logger.warning("$connect event without a connectionId; rejecting")
        return {"statusCode": 400}

    connections.add_connection(connection_id)
    logger.info("ws connected: %s", connection_id)
    return {"statusCode": 200}
