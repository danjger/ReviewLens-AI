"""The ``ws-connections`` DynamoDB store (dataset-library Requirement 6).

One place touches the WebSocket connection table, so the connect/disconnect
handlers and the push consumer never build a DynamoDB client by hand or drift
on the item shape. The table (PK ``connection_id``, TTL ``ttl``) is provisioned
by ``platform-foundation`` (``infra/lib/data-stack.ts``); this module only reads
its name from :class:`app.core.config.Settings`.

Item shape::

    { connection_id, connected_at (ISO-8601 UTC), ttl (epoch seconds, +2h) }

Everything is stateless: the only cached value is the boto3 client handle (a
connection pool), so no correctness depends on process memory. The client
honours ``aws_endpoint_url`` for LocalStack, matching
:mod:`app.core.rate_limit`, :mod:`app.events.publisher`, and
:mod:`app.ingestion.check_session`.

Privacy: a connection ID is an opaque API Gateway token, not a client IP, so
nothing here stores or logs a raw address.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from app.core.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

logger = logging.getLogger(__name__)

#: How long a connection row lives before DynamoDB's TTL sweep reaps it. This is
#: a backstop for connections whose ``$disconnect`` never arrived; a live
#: browser reconnects well within this window.
CONNECTION_TTL_SECONDS = 2 * 60 * 60

_client: DynamoDBClient | None = None


def _get_client() -> DynamoDBClient:
    """Return a shared DynamoDB client, honouring ``aws_endpoint_url``."""
    global _client
    if _client is None:
        settings = get_settings()
        endpoint_url = settings.aws_endpoint_url or None
        _client = boto3.client("dynamodb", endpoint_url=endpoint_url)
    return _client


def reset_client() -> None:
    """Clear the cached DynamoDB client.

    Intended for tests that enter a moto ``mock_aws`` context or change settings
    between cases. Safe to call when no client has been created.
    """
    global _client
    _client = None


def _table_name() -> str:
    return get_settings().ws_connections_table


def add_connection(connection_id: str, *, now: float | None = None) -> None:
    """Record a newly connected WebSocket client (idempotent).

    Writing the same ``connection_id`` twice simply refreshes ``connected_at``
    and the TTL, so a duplicate ``$connect`` is harmless.
    """
    seconds = time.time() if now is None else now
    _get_client().put_item(
        TableName=_table_name(),
        Item={
            "connection_id": {"S": connection_id},
            "connected_at": {"S": datetime.fromtimestamp(seconds, UTC).isoformat()},
            "ttl": {"N": str(int(seconds) + CONNECTION_TTL_SECONDS)},
        },
    )
    logger.debug("ws connection added: %s", connection_id)


def remove_connection(connection_id: str) -> None:
    """Delete a connection row (idempotent).

    Used by ``$disconnect`` and by the push consumer when a send returns
    ``GoneException``. Deleting a row that is already gone is a no-op, so
    duplicate or concurrent deletes are safe.
    """
    try:
        _get_client().delete_item(
            TableName=_table_name(),
            Key={"connection_id": {"S": connection_id}},
        )
        logger.debug("ws connection removed: %s", connection_id)
    except ClientError:
        # A delete that races another delete (or a vanished table during
        # teardown) must never fail the caller: the row is gone either way.
        logger.warning("ws connection delete failed for %s", connection_id, exc_info=True)


def list_connection_ids() -> list[str]:
    """Return every active connection ID.

    The table is scanned with pagination so a large fan-out still sees every
    connection. There is no index to query — the push broadcast targets *all*
    connections (the app has no sign-in, so everyone sees the same datasets),
    which is exactly a full scan.
    """
    client = _get_client()
    table = _table_name()
    ids: list[str] = []
    start_key: dict[str, object] | None = None
    while True:
        kwargs: dict[str, object] = {
            "TableName": table,
            "ProjectionExpression": "connection_id",
        }
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key
        response = client.scan(**kwargs)  # type: ignore[arg-type]
        for item in response.get("Items", []):
            cid = item.get("connection_id", {}).get("S")
            if cid:
                ids.append(cid)
        start_key = response.get("LastEvaluatedKey")  # type: ignore[assignment]
        if not start_key:
            break
    return ids
