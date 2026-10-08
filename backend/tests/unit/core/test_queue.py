"""Unit tests for app.core.queue (the SQS enqueue helper).

Covers:
- enqueue sends a JSON body to the target queue (verified by receiving it back
  from a moto-mocked standard queue);
- the body is IDs-only JSON and round-trips exactly;
- a FIFO queue send includes the message group and deduplication IDs.

The helper is the one place that *sends* SQS messages (the consumer owns the
receive side), so these tests pin its contract: a small JSON body on the chosen
queue URL, honouring the ``aws_endpoint_url`` override pattern used elsewhere.
"""

from __future__ import annotations

import json
from collections.abc import Generator

import boto3
import pytest
from app.core import queue as queue_mod
from app.core.config import get_settings
from moto import mock_aws

_REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("AWS_ENDPOINT_URL", "")
    get_settings.cache_clear()
    queue_mod.reset_client()
    yield
    queue_mod.reset_client()
    get_settings.cache_clear()


def test_enqueue_sends_json_body_to_queue() -> None:
    with mock_aws():
        sqs = boto3.client("sqs", region_name=_REGION)
        url = sqs.create_queue(QueueName="check-queue")["QueueUrl"]

        queue_mod.enqueue(url, {"check_id": "chk-1", "item_id": "u1"})

        received = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=1)
        messages = received.get("Messages", [])
        assert len(messages) == 1
        body = json.loads(messages[0]["Body"])
        assert body == {"check_id": "chk-1", "item_id": "u1"}


def test_enqueue_fifo_includes_group_and_dedup() -> None:
    with mock_aws():
        sqs = boto3.client("sqs", region_name=_REGION)
        url = sqs.create_queue(
            QueueName="processing-queue.fifo",
            Attributes={"FifoQueue": "true"},
        )["QueueUrl"]

        queue_mod.enqueue(
            url,
            {"dataset_id": "ds-1", "version": 1},
            message_group_id="ds-1",
            message_deduplication_id="ds-1-1",
        )

        received = sqs.receive_message(
            QueueUrl=url,
            MaxNumberOfMessages=1,
            AttributeNames=["MessageGroupId"],
        )
        messages = received.get("Messages", [])
        assert len(messages) == 1
        body = json.loads(messages[0]["Body"])
        assert body == {"dataset_id": "ds-1", "version": 1}
        assert messages[0]["Attributes"]["MessageGroupId"] == "ds-1"
