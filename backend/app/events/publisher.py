"""EventBridge publisher for ReviewLens AI domain events.

This is the one place that puts events onto the EventBridge bus. Every domain
event (``dataset.status.changed``, ``check.updated``, ``chat.exchange.saved``)
flows through :func:`publish_event`, which keeps the ``Source``,
``EventBusName``, and serialization consistent and honours the LocalStack
endpoint override used by the local stack and integration tests.

The event detail is a plain JSON-serialisable ``dict``. Callers pass the
EventBridge *detail-type* (for example ``dataset.status.changed``) and the
detail payload; the publisher adds the common ``Source`` and target bus.

Usage::

    from app.events.publisher import publish_event

    publish_event(
        detail_type="dataset.status.changed",
        detail={"dataset_id": ds_id, "status": "processing", ...},
    )

The client is memoised per process. Call :func:`reset_client` in tests after
changing settings or entering a moto ``mock_aws`` context.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

import boto3

from app.core.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_events import EventBridgeClient

logger = logging.getLogger(__name__)

#: EventBridge ``Source`` applied to every event this application publishes.
EVENT_SOURCE = "reviewlens"

_client: EventBridgeClient | None = None


def _get_events_client() -> EventBridgeClient:
    """Return a shared EventBridge client, honouring ``aws_endpoint_url``.

    The endpoint override lets LocalStack (local stack and integration tests)
    receive events; in AWS the override is empty and the default endpoint is
    used.
    """
    global _client
    if _client is None:
        settings = get_settings()
        endpoint_url = settings.aws_endpoint_url or None
        _client = cast(
            "EventBridgeClient",
            boto3.client("events", endpoint_url=endpoint_url),
        )
    return _client


def reset_client() -> None:
    """Clear the cached EventBridge client.

    Intended for tests that enter a moto ``mock_aws`` context or change
    settings between cases. Safe to call when no client has been created.
    """
    global _client
    _client = None


def publish_event(detail_type: str, detail: dict[str, Any]) -> None:
    """Publish a single domain event to the EventBridge bus.

    Args:
        detail_type: The EventBridge *detail-type* identifying the event, e.g.
            ``dataset.status.changed``.
        detail: JSON-serialisable payload for the event's ``Detail`` field.

    The event is tagged with the common :data:`EVENT_SOURCE` and sent to the
    bus named by ``Settings.eventbridge_bus_name``. The payload is serialised
    with ``default=str`` so values such as ``datetime`` serialise predictably.
    """
    settings = get_settings()
    client = _get_events_client()
    client.put_events(
        Entries=[
            {
                "Source": EVENT_SOURCE,
                "DetailType": detail_type,
                "Detail": json.dumps(detail, default=str),
                "EventBusName": settings.eventbridge_bus_name,
            }
        ]
    )
    logger.debug("Published %s to bus %s", detail_type, settings.eventbridge_bus_name)
