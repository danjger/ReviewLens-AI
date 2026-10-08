"""SQS enqueue helper for ReviewLens AI.

This is the one place that *sends* messages onto the application's SQS queues.
The consumer runtime (:mod:`app.consumer`) owns the *receive* side; this module
owns the *send* side so the two never drift and so no other module builds an
SQS client by hand.

Queue message bodies are small JSON objects carrying IDs only (an engineering
rule): the payload itself lives in S3 or the database. For the check queue a
message is ``{"check_id": ..., "item_id": ...}``; the worker reads the rest of
the item from the Check Session.

The client is memoised per process and honours the ``aws_endpoint_url`` override
used by LocalStack in the local stack and integration tests, matching the
pattern already used by :mod:`app.storage.s3`, :mod:`app.events.publisher`, and
:mod:`app.core.rate_limit`.

Nothing here depends on process memory for correctness — the only cached value
is the boto3 client handle (a connection pool), so the module stays stateless.

FIFO support: when ``message_group_id`` is given the send includes it (and a
derived or supplied ``message_deduplication_id``) so the same helper serves the
standard ``check-queue`` and the FIFO ``processing-queue``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

import boto3

from app.core.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_sqs import SQSClient

logger = logging.getLogger(__name__)

_client: SQSClient | None = None


def _get_sqs_client() -> SQSClient:
    """Return a shared SQS client, honouring ``aws_endpoint_url`` for LocalStack."""
    global _client
    if _client is None:
        settings = get_settings()
        endpoint_url = settings.aws_endpoint_url or None
        _client = cast("SQSClient", boto3.client("sqs", endpoint_url=endpoint_url))
    return _client


def reset_client() -> None:
    """Clear the cached SQS client.

    Intended for tests that enter a moto ``mock_aws`` context or change settings
    between cases. Safe to call when no client has been created.
    """
    global _client
    _client = None


def enqueue(
    queue_url: str,
    body: dict[str, Any],
    *,
    message_group_id: str | None = None,
    message_deduplication_id: str | None = None,
) -> None:
    """Send one JSON message to *queue_url*.

    Args:
        queue_url: The target queue URL (from ``Settings`` / env, never a
            literal). The caller chooses the queue so this helper stays generic.
        body: A small JSON-serialisable dict carrying IDs only. It is serialised
            with ``default=str`` so values such as UUIDs serialise predictably.
        message_group_id: FIFO message group ID. Supply only for FIFO queues
            (for example the processing queue); omit for standard queues like
            the check queue.
        message_deduplication_id: FIFO deduplication ID. Only used when
            ``message_group_id`` is given; omit to rely on the queue's
            content-based deduplication.

    The message body shape is the queue's contract; this helper does not inspect
    or validate it beyond JSON serialisation.
    """
    client = _get_sqs_client()
    kwargs: dict[str, Any] = {
        "QueueUrl": queue_url,
        "MessageBody": json.dumps(body, default=str),
    }
    if message_group_id is not None:
        kwargs["MessageGroupId"] = message_group_id
        if message_deduplication_id is not None:
            kwargs["MessageDeduplicationId"] = message_deduplication_id

    client.send_message(**kwargs)
    logger.debug("enqueued message to %s: %s", queue_url, body)
