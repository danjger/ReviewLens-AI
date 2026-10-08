"""Integration test for the real-time push path (dataset-library task 4.3).

Drives the design's fan-out path end to end against LocalStack:

    publish_event() ──▶ EventBridge bus ──rule──▶ push-queue (dedicated test
    queue) ──▶ PushHandler.handle() ──▶ postToConnection ──▶ ws-connections
    cleanup

What is real here:
* the EventBridge bus and an EventBridge *rule* matching the two broadcast
  detail types (the same pattern the RealtimeStack deploys), targeting a
  **dedicated test queue** so the test never races the live ``push-consumer``
  container for the shared ``push-queue``;
* the ``ws-connections`` DynamoDB table (seeded connections, stale cleanup);
* the real ``PushHandler`` run in-process on the message pulled off the queue.

What is stubbed and why: LocalStack's ``apigatewaymanagementapi``
(``postToConnection``) support is limited, so the management-API client is
replaced with a fake that records targets and can return ``GoneException``. The
assertions are on durable state — which connections were targeted and which
``ws-connections`` rows survive — not on timing, matching the testing
conventions (Requirements 6.1, 6.2, 6.5).

Run with ``make test-int``. Skips cleanly when LocalStack is not reachable.
Each test uses its own connection IDs and its own test queue, and cleans them up.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import httpx
import pytest
from app.consumer import MessageMeta
from app.core.config import get_settings
from app.events.publisher import publish_event
from app.handlers import push as push_mod
from app.handlers.push import PushHandler
from app.realtime import connections
from botocore.exceptions import ClientError

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_ENDPOINT = "http://localhost:4566"
_BUS = "reviewlens-events"
_TABLE = "ws-connections"


def _localstack_up() -> bool:
    try:
        resp = httpx.get(f"{_ENDPOINT}/_localstack/health", timeout=2.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_stack() -> None:
    if not _localstack_up():
        pytest.skip("LocalStack not reachable; run under `make test-int`")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ENDPOINT_URL", _ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("EVENTBRIDGE_BUS_NAME", _BUS)
    monkeypatch.setenv("WS_CONNECTIONS_TABLE", _TABLE)
    get_settings.cache_clear()
    connections.reset_client()
    push_mod.reset_client()
    from app.events import publisher

    publisher.reset_client()
    try:
        yield
    finally:
        get_settings.cache_clear()
        connections.reset_client()
        push_mod.reset_client()
        publisher.reset_client()


@pytest.fixture()
def test_queue() -> Iterator[str]:
    """A dedicated push test queue, wired to the bus by an EventBridge rule.

    Using a per-test queue (not the shared ``push-queue``) means the live
    ``push-consumer`` container never steals the message under test. The rule
    matches the same detail types the RealtimeStack deploys.
    """
    sqs = boto3.client("sqs", region_name=_REGION, endpoint_url=_ENDPOINT)
    events = boto3.client("events", region_name=_REGION, endpoint_url=_ENDPOINT)

    suffix = uuid.uuid4().hex[:12]
    queue_name = f"push-it-{suffix}"
    rule_name = f"push-it-rule-{suffix}"

    queue_url = sqs.create_queue(QueueName=queue_name)["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]

    events.put_rule(
        Name=rule_name,
        EventBusName=_BUS,
        EventPattern=json.dumps(
            {
                "source": ["reviewlens"],
                "detail-type": ["dataset.status.changed", "check.updated"],
            }
        ),
    )
    events.put_targets(
        Rule=rule_name,
        EventBusName=_BUS,
        Targets=[{"Id": "push-test-queue", "Arn": queue_arn}],
    )
    try:
        yield queue_url
    finally:
        events.remove_targets(Rule=rule_name, EventBusName=_BUS, Ids=["push-test-queue"])
        events.delete_rule(Name=rule_name, EventBusName=_BUS)
        sqs.delete_queue(QueueUrl=queue_url)


@pytest.fixture()
def seeded_connections() -> Iterator[list[str]]:
    """Seed three ws-connections rows and remove them afterwards."""
    ids = [f"it-conn-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    for cid in ids:
        connections.add_connection(cid)
    try:
        yield ids
    finally:
        for cid in ids:
            connections.remove_connection(cid)


class _FakeMgmt:
    """Fake apigatewaymanagementapi client (LocalStack support is limited)."""

    def __init__(self, gone: set[str]) -> None:
        self.gone = gone
        self.posted: list[str] = []

    def post_to_connection(self, *, ConnectionId: str, Data: bytes) -> None:  # noqa: N803
        if ConnectionId in self.gone:
            raise ClientError(
                {"Error": {"Code": "GoneException", "Message": "gone"}},
                "PostToConnection",
            )
        self.posted.append(ConnectionId)


def _drain_one(queue_url: str) -> dict[str, Any]:
    """Pull exactly one EventBridge message off the test queue (polls briefly)."""
    sqs = boto3.client("sqs", region_name=_REGION, endpoint_url=_ENDPOINT)
    deadline = time.time() + 15
    while time.time() < deadline:
        resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=2)
        messages = resp.get("Messages", [])
        if messages:
            msg = messages[0]
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
            return json.loads(msg["Body"])
    raise AssertionError("no message arrived on the push test queue within 15s")


def test_event_fans_out_and_cleans_up_stale_connection(
    monkeypatch: pytest.MonkeyPatch,
    test_queue: str,
    seeded_connections: list[str],
) -> None:
    live_a, live_b, stale = seeded_connections
    fake = _FakeMgmt(gone={stale})
    monkeypatch.setattr(push_mod, "_get_mgmt_client", lambda: fake)

    # Publish a real domain event; the EventBridge rule routes it to our queue.
    publish_event(
        detail_type="dataset.status.changed",
        detail={"dataset_id": "d-int", "status": "updated"},
    )

    body = _drain_one(test_queue)
    assert body["detail-type"] == "dataset.status.changed"

    # Run the real handler on the message the bus delivered.
    PushHandler().handle(body, MessageMeta(message_id="it", receive_count=1))

    # Both live connections were posted to; the stale one was reaped.
    assert set(fake.posted) == {live_a, live_b}
    remaining = set(connections.list_connection_ids())
    assert stale not in remaining
    assert {live_a, live_b} <= remaining


def test_check_updated_event_is_also_routed_and_broadcast(
    monkeypatch: pytest.MonkeyPatch,
    test_queue: str,
    seeded_connections: list[str],
) -> None:
    fake = _FakeMgmt(gone=set())
    monkeypatch.setattr(push_mod, "_get_mgmt_client", lambda: fake)

    publish_event(
        detail_type="check.updated",
        detail={"check_id": "c-int", "item_id": "u1", "verdict": "will_work"},
    )

    body = _drain_one(test_queue)
    assert body["detail-type"] == "check.updated"

    PushHandler().handle(body, MessageMeta(message_id="it", receive_count=1))

    assert set(fake.posted) == set(seeded_connections)
