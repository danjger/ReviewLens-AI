"""Property-based tests for app.consumer (the queue consumer runtime).

Property 4: Batch failure reporting.
  For any batch of SQS messages and any pattern of handler successes and
  failures, the Lambda adapter SHALL report exactly the failed message IDs,
  and the poller SHALL delete exactly the succeeded messages.
  Validates: Requirements 3.4, 3.5

Both halves of the property are exercised here against arbitrary batches and
arbitrary success/failure patterns:

- the Lambda adapter (``app.consumer.lambda_entry``) must return exactly the
  failed message IDs in ``batchItemFailures``;
- the container poller (``app.consumer.run_poller``) must delete exactly the
  succeeded messages and leave the failed ones in the queue.

The poller is driven with the same in-memory ``FakeSqs`` / recording-handler
approach used by ``tests/unit/test_consumer.py`` (monkeypatch ``_load_handler``
and ``_build_sqs_client``, drive the loop with a shutdown event), so the test
runs with no AWS and no database.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from app import consumer
from app.consumer import MessageMeta, lambda_entry, run_poller
from hypothesis import given, settings
from hypothesis import strategies as st

_REGION = "us-east-1"
_QUEUE = "check"


# ---------------------------------------------------------------------------
# Test doubles (mirroring tests/unit/test_consumer.py)
# ---------------------------------------------------------------------------


@dataclass
class RecordingHandler:
    """A Handler that records calls and fails for a configured set of ids."""

    queue: str = _QUEUE
    calls: list[tuple[dict[str, Any], MessageMeta]] = field(default_factory=list)
    fail_bodies: set[str] = field(default_factory=set)

    def handle(self, body: dict[str, Any], meta: MessageMeta) -> None:
        self.calls.append((body, meta))
        if body.get("id") in self.fail_bodies:
            raise RuntimeError(f"boom for {body.get('id')!r}")


class FakeSqs:
    """A minimal in-memory SQS stand-in that records deletes.

    ``receive_message`` returns queued messages one at a time; when empty it
    briefly blocks so the driving thread can set the shutdown event.
    """

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = list(messages)
        self.visibility_calls: list[dict[str, Any]] = []
        self.deleted_receipts: list[str] = []

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        if self._messages:
            return {"Messages": [self._messages.pop(0)]}
        time.sleep(min(0.02, float(kwargs.get("WaitTimeSeconds", 0)) or 0.02))
        return {"Messages": []}

    def change_message_visibility(self, **kwargs: Any) -> None:
        self.visibility_calls.append(kwargs)

    def delete_message(self, **kwargs: Any) -> None:
        self.deleted_receipts.append(kwargs["ReceiptHandle"])


def _sqs_record(message_id: str, body: dict[str, Any], receive_count: int = 1) -> dict[str, Any]:
    """Build an SQS event record as the Lambda event-source mapping delivers it."""
    return {
        "messageId": message_id,
        "receiptHandle": f"rh-{message_id}",
        "body": json.dumps(body),
        "attributes": {"ApproximateReceiveCount": str(receive_count)},
        "eventSourceARN": f"arn:aws:sqs:{_REGION}:123456789012:{_QUEUE}-queue",
    }


def _poller_message(
    message_id: str, body: dict[str, Any], receive_count: int = 1
) -> dict[str, Any]:
    """Build a message in the shape ``receive_message`` returns."""
    return {
        "MessageId": message_id,
        "ReceiptHandle": f"rh-{message_id}",
        "Body": json.dumps(body),
        "Attributes": {"ApproximateReceiveCount": str(receive_count)},
    }


# A batch is an arbitrary list of success/failure outcomes; outcomes[i] == True
# means message i's handler should raise.
_outcomes = st.lists(st.booleans(), min_size=0, max_size=12)


# ---------------------------------------------------------------------------
# Property 4 — Lambda adapter half
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(outcomes=_outcomes)
def test_lambda_reports_exactly_failed_ids(outcomes: list[bool]) -> None:
    """Property 4: Batch failure reporting (Lambda adapter half).

    For any batch of SQS messages and any pattern of handler successes and
    failures, the Lambda adapter reports exactly the failed message IDs.
    Validates: Requirements 3.4, 3.5
    """
    fail_ids = {f"id-{i}" for i, failed in enumerate(outcomes) if failed}
    handler = RecordingHandler(fail_bodies=fail_ids)
    records = [_sqs_record(f"m-{i}", {"id": f"id-{i}"}) for i in range(len(outcomes))]

    with patch.object(consumer, "_load_handler", lambda _q: handler):
        result = lambda_entry({"Records": records}, context=None)

    reported = {f["itemIdentifier"] for f in result["batchItemFailures"]}
    expected = {f"m-{i}" for i, failed in enumerate(outcomes) if failed}
    assert reported == expected


# ---------------------------------------------------------------------------
# Property 4 — poller half
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(outcomes=_outcomes)
def test_poller_deletes_exactly_succeeded_messages(outcomes: list[bool]) -> None:
    """Property 4: Batch failure reporting (poller half).

    For any batch and any success/failure pattern, the poller deletes exactly
    the succeeded messages and leaves the failed ones in the queue.
    Validates: Requirements 3.4, 3.5
    """
    fail_ids = {f"id-{i}" for i, failed in enumerate(outcomes) if failed}
    handler = RecordingHandler(fail_bodies=fail_ids)
    messages = [_poller_message(f"m-{i}", {"id": f"id-{i}"}) for i in range(len(outcomes))]
    fake = FakeSqs(messages)

    shutdown = threading.Event()

    # Drive the loop until every message has been handled at least once, then
    # stop. A big heartbeat interval keeps visibility churn out of the way.
    def _watch() -> None:
        while len(handler.calls) < len(outcomes):
            time.sleep(0.005)
        time.sleep(0.03)
        shutdown.set()

    with (
        patch.object(consumer, "_load_handler", lambda _q: handler),
        patch.object(consumer, "_build_sqs_client", lambda: fake),
        patch.object(consumer, "HEARTBEAT_INTERVAL_SECONDS", 3600),
        patch.dict("os.environ", {"CHECK_QUEUE_URL": "http://local/check"}),
    ):
        if not outcomes:
            # Nothing to process: just stop the loop promptly.
            shutdown.set()
            run_poller(_QUEUE, shutdown=shutdown)
        else:
            watcher = threading.Thread(target=_watch)
            watcher.start()
            run_poller(_QUEUE, shutdown=shutdown)
            watcher.join()

    deleted = set(fake.deleted_receipts)
    expected_deleted = {f"rh-m-{i}" for i, failed in enumerate(outcomes) if not failed}
    assert deleted == expected_deleted
