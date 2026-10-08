"""Unit tests for app.consumer (the queue consumer runtime).

Covers task 4.2 for the platform-foundation spec:
- Both adapters (``lambda_entry`` and ``run_poller``) calling the SAME handler
  with the same body and meta — one code path for both compute modes.
- Final-attempt detection from the receive count
  (``MessageMeta.is_final_attempt``).
- Lambda partial batch failures: ``batchItemFailures`` contains exactly the
  failed message IDs (Property 4 territory).
- Poller deletes exactly the succeeded messages and leaves failed ones in the
  queue (Property 4 territory).
- Heartbeat extension: visibility is extended while a handler is working.
- Shutdown mid-message: a shutdown event set during processing finishes the
  current message then stops; set during the long poll it does not begin a new
  message.

_Validates: Requirements 3.4, 3.5, 3.7_

moto provides a real-ish SQS for the poller delete/leave tests; the heartbeat
and shutdown tests use a hand-built fake SQS client so timing is deterministic
and fast (the real interval constants are patched down to milliseconds).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import boto3
import pytest
from app import consumer
from app.consumer import (
    Handler,
    MessageMeta,
    lambda_entry,
    run_poller,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

_REGION = "us-east-1"
_QUEUE = "check"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class RecordingHandler:
    """A Handler that records every (body, meta) it receives.

    ``fail_bodies`` holds the bodies for which ``handle`` should raise, so a
    single handler can be driven through mixed success/failure batches.
    """

    queue: str = _QUEUE
    calls: list[tuple[dict[str, Any], MessageMeta]] = field(default_factory=list)
    fail_bodies: set[str] = field(default_factory=set)

    def handle(self, body: dict[str, Any], meta: MessageMeta) -> None:
        self.calls.append((body, meta))
        if body.get("id") in self.fail_bodies:
            raise RuntimeError(f"boom for {body.get('id')!r}")


@dataclass
class BlockingHandler:
    """A Handler that blocks inside ``handle`` until ``release`` is set.

    Lets a test observe the heartbeat firing while a handler is "working".
    ``entered`` is set as soon as ``handle`` begins.
    """

    queue: str = _QUEUE
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    calls: list[tuple[dict[str, Any], MessageMeta]] = field(default_factory=list)

    def handle(self, body: dict[str, Any], meta: MessageMeta) -> None:
        self.calls.append((body, meta))
        self.entered.set()
        # Wait, but never hang the suite forever if something goes wrong.
        self.release.wait(timeout=10)


class FakeSqs:
    """A minimal in-memory SQS stand-in for deterministic poller tests.

    ``receive_message`` returns queued messages one at a time; when empty it
    blocks for the requested wait so the test can set shutdown during a poll.
    ``change_message_visibility`` / ``delete_message`` just record calls.
    """

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = list(messages)
        self.visibility_calls: list[dict[str, Any]] = []
        self.deleted_receipts: list[str] = []
        self.receive_calls = 0

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        self.receive_calls += 1
        if self._messages:
            return {"Messages": [self._messages.pop(0)]}
        # Emulate a long poll that returns empty after the wait elapses.
        time.sleep(min(0.02, float(kwargs.get("WaitTimeSeconds", 0)) or 0.02))
        return {"Messages": []}

    def change_message_visibility(self, **kwargs: Any) -> None:
        self.visibility_calls.append(kwargs)

    def delete_message(self, **kwargs: Any) -> None:
        self.deleted_receipts.append(kwargs["ReceiptHandle"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal AWS env so boto3 never reaches for real credentials."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


# ===========================================================================
# MessageMeta: final-attempt detection
# ===========================================================================


class TestMessageMetaFinalAttempt:
    """is_final_attempt is driven purely by the receive count.

    _Validates: Requirement 3.5_
    """

    def test_not_final_below_max(self) -> None:
        for count in range(1, consumer.MAX_RECEIVE_COUNT):
            meta = MessageMeta(message_id="m", receive_count=count)
            assert meta.is_final_attempt is False

    def test_final_at_max(self) -> None:
        meta = MessageMeta(message_id="m", receive_count=consumer.MAX_RECEIVE_COUNT)
        assert meta.is_final_attempt is True

    def test_final_above_max(self) -> None:
        meta = MessageMeta(message_id="m", receive_count=consumer.MAX_RECEIVE_COUNT + 5)
        assert meta.is_final_attempt is True

    def test_first_delivery_is_not_final(self) -> None:
        assert MessageMeta(message_id="m", receive_count=1).is_final_attempt is False


# ===========================================================================
# Both adapters call the SAME handler with the SAME body and meta
# ===========================================================================


class TestBothAdaptersShareOneHandler:
    """lambda_entry and run_poller drive the identical handler code path.

    _Validates: Requirements 3.4, 3.7_
    """

    def test_same_handler_same_body_and_meta_in_both_modes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler = RecordingHandler()
        body = {"id": "d1", "payload": "x"}
        receive_count = 2

        # One shared handler instance serves both adapters.
        monkeypatch.setattr(consumer, "_load_handler", lambda _queue: handler)

        # Lambda adapter.
        lambda_entry({"Records": [_sqs_record("m1", body, receive_count)]}, context=None)

        # Container poller: one message, then shutdown so the loop exits.
        shutdown = threading.Event()
        fake = FakeSqs([_poller_message("m1", body, receive_count)])
        monkeypatch.setattr(consumer, "_build_sqs_client", lambda: fake)
        monkeypatch.setenv("CHECK_QUEUE_URL", "http://local/check")

        def _stop_after_first() -> None:
            # Let the single message be processed, then end the loop.
            time.sleep(0.05)
            shutdown.set()

        t = threading.Thread(target=_stop_after_first)
        t.start()
        run_poller(_QUEUE, shutdown=shutdown)
        t.join()

        # Exactly two invocations: one per adapter, same body, same meta.
        assert len(handler.calls) == 2
        (body_a, meta_a), (body_b, meta_b) = handler.calls
        assert body_a == body_b == body
        assert meta_a == meta_b
        assert meta_a == MessageMeta(message_id="m1", receive_count=receive_count)


# ===========================================================================
# Lambda partial batch failures (Property 4)
# ===========================================================================


class TestLambdaBatchFailures:
    """lambda_entry reports exactly the failed message IDs.

    _Validates: Requirements 3.4, 3.5_
    """

    def test_all_success_reports_no_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        handler = RecordingHandler()
        monkeypatch.setattr(consumer, "_load_handler", lambda _q: handler)
        event = {
            "Records": [
                _sqs_record("m1", {"id": "a"}),
                _sqs_record("m2", {"id": "b"}),
            ]
        }
        result = lambda_entry(event, context=None)
        assert result == {"batchItemFailures": []}

    def test_mixed_batch_reports_only_failed_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        handler = RecordingHandler(fail_bodies={"b", "d"})
        monkeypatch.setattr(consumer, "_load_handler", lambda _q: handler)
        event = {
            "Records": [
                _sqs_record("m1", {"id": "a"}),
                _sqs_record("m2", {"id": "b"}),
                _sqs_record("m3", {"id": "c"}),
                _sqs_record("m4", {"id": "d"}),
            ]
        }
        result = lambda_entry(event, context=None)
        failed_ids = {f["itemIdentifier"] for f in result["batchItemFailures"]}
        assert failed_ids == {"m2", "m4"}

    def test_empty_batch_reports_no_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No _load_handler should be needed for an empty batch.
        result = lambda_entry({"Records": []}, context=None)
        assert result == {"batchItemFailures": []}

    def test_malformed_json_body_is_reported_as_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler = RecordingHandler()
        monkeypatch.setattr(consumer, "_load_handler", lambda _q: handler)
        bad = {
            "messageId": "m1",
            "receiptHandle": "rh-m1",
            "body": "{not json",
            "attributes": {"ApproximateReceiveCount": "1"},
            "eventSourceARN": f"arn:aws:sqs:{_REGION}:1:{_QUEUE}-queue",
        }
        result = lambda_entry({"Records": [bad]}, context=None)
        assert result["batchItemFailures"] == [{"itemIdentifier": "m1"}]


# ---------------------------------------------------------------------------
# Property 4: Batch failure reporting (Lambda adapter half)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(
    outcomes=st.lists(st.booleans(), min_size=0, max_size=10),
)
def test_property4_lambda_reports_exactly_failed_ids(outcomes: list[bool]) -> None:
    """Property 4: Batch failure reporting.

    For any batch of SQS messages and any pattern of handler successes and
    failures, the Lambda adapter reports exactly the failed message IDs.
    _Validates: Requirements 3.4, 3.5_
    """
    # outcomes[i] == True means message i should FAIL.
    fail_ids = {f"id-{i}" for i, failed in enumerate(outcomes) if failed}
    handler = RecordingHandler(fail_bodies=fail_ids)
    records = [_sqs_record(f"m-{i}", {"id": f"id-{i}"}) for i in range(len(outcomes))]

    with patch.object(consumer, "_load_handler", lambda _q: handler):
        result = lambda_entry({"Records": records}, context=None)

    reported = {f["itemIdentifier"] for f in result["batchItemFailures"]}
    expected = {f"m-{i}" for i, failed in enumerate(outcomes) if failed}
    assert reported == expected


# ===========================================================================
# Poller: deletes succeeded messages, leaves failed ones (Property 4 half)
# ===========================================================================


class TestPollerDeleteOnSuccess:
    """run_poller deletes exactly the messages whose handler succeeded.

    Uses moto's SQS so the leave-on-failure behaviour is observable: a failed
    message stays in the queue (becomes visible again) rather than being
    deleted. _Validates: Requirements 3.4, 3.5_
    """

    @mock_aws
    def test_success_is_deleted_failure_is_left(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sqs = boto3.client("sqs", region_name=_REGION)
        # Zero visibility timeout so a left (failed) message is immediately
        # visible again for assertion.
        queue_url = sqs.create_queue(
            QueueName="check-queue",
            Attributes={"VisibilityTimeout": "0"},
        )["QueueUrl"]
        monkeypatch.setenv("CHECK_QUEUE_URL", queue_url)

        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps({"id": "ok"}))
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps({"id": "bad"}))

        handler = RecordingHandler(fail_bodies={"bad"})
        monkeypatch.setattr(consumer, "_load_handler", lambda _q: handler)
        # Avoid background heartbeat churn against moto; keep intervals large.
        monkeypatch.setattr(consumer, "HEARTBEAT_INTERVAL_SECONDS", 3600)

        shutdown = threading.Event()
        processed = threading.Event()

        # Stop once both messages have been handled at least once.
        def _watch() -> None:
            while len(handler.calls) < 2 and not processed.wait(0.02):
                pass
            processed.set()
            shutdown.set()

        # Short long-poll so the loop spins quickly in the test.
        monkeypatch.setattr(consumer, "LONG_POLL_WAIT_SECONDS", 1)
        t = threading.Thread(target=_watch)
        t.start()
        run_poller(_QUEUE, shutdown=shutdown)
        t.join()

        handled_ids = {body["id"] for body, _ in handler.calls}
        assert "ok" in handled_ids and "bad" in handled_ids

        # The failed message must still be retrievable; the succeeded one gone.
        remaining: set[str] = set()
        for _ in range(5):
            resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10)
            for m in resp.get("Messages", []):
                remaining.add(json.loads(m["Body"])["id"])
        assert "bad" in remaining
        assert "ok" not in remaining


# ===========================================================================
# Heartbeat: visibility extended while a handler is working
# ===========================================================================


class TestHeartbeatExtendsVisibility:
    """While a handler works, the poller extends the message's visibility.

    _Validates: Requirement 3.7_
    """

    def test_visibility_extended_during_processing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = {"id": "slow"}
        handler = BlockingHandler()
        fake = FakeSqs([_poller_message("m1", body)])

        monkeypatch.setattr(consumer, "_load_handler", lambda _q: handler)
        monkeypatch.setattr(consumer, "_build_sqs_client", lambda: fake)
        monkeypatch.setenv("CHECK_QUEUE_URL", "http://local/check")
        # Fire the heartbeat almost immediately and repeatedly.
        monkeypatch.setattr(consumer, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(consumer, "VISIBILITY_EXTENSION_SECONDS", 90)

        shutdown = threading.Event()
        poller = threading.Thread(target=run_poller, args=(_QUEUE,), kwargs={"shutdown": shutdown})
        poller.start()

        # Wait until the handler is mid-flight, then let heartbeats accumulate.
        assert handler.entered.wait(timeout=5)
        time.sleep(0.1)

        # Release the handler and stop the loop.
        handler.release.set()
        shutdown.set()
        poller.join(timeout=5)

        assert len(fake.visibility_calls) >= 1
        first = fake.visibility_calls[0]
        assert first["ReceiptHandle"] == "rh-m1"
        assert first["VisibilityTimeout"] == 90

    def test_heartbeat_stops_after_handler_finishes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = {"id": "quick"}
        handler = RecordingHandler()
        fake = FakeSqs([_poller_message("m1", body)])

        monkeypatch.setattr(consumer, "_load_handler", lambda _q: handler)
        monkeypatch.setattr(consumer, "_build_sqs_client", lambda: fake)
        monkeypatch.setenv("CHECK_QUEUE_URL", "http://local/check")
        monkeypatch.setattr(consumer, "HEARTBEAT_INTERVAL_SECONDS", 0.01)

        shutdown = threading.Event()

        def _stop() -> None:
            time.sleep(0.1)
            shutdown.set()

        t = threading.Thread(target=_stop)
        t.start()
        run_poller(_QUEUE, shutdown=shutdown)
        t.join()

        # The quick handler finished promptly, so the heartbeat thread for it
        # should have stopped; no extension needed to be recorded.
        assert len(handler.calls) == 1
        count_after_finish = len(fake.visibility_calls)
        time.sleep(0.05)
        assert len(fake.visibility_calls) == count_after_finish


# ===========================================================================
# Shutdown mid-message and during the long poll
# ===========================================================================


class TestGracefulShutdown:
    """SIGTERM/shutdown finishes the current message then stops, and never
    begins a new message once shutdown is set.

    _Validates: Requirement 3.7_
    """

    def test_shutdown_during_processing_finishes_current_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler = BlockingHandler()
        fake = FakeSqs(
            [
                _poller_message("m1", {"id": "first"}),
                _poller_message("m2", {"id": "second"}),
            ]
        )
        monkeypatch.setattr(consumer, "_load_handler", lambda _q: handler)
        monkeypatch.setattr(consumer, "_build_sqs_client", lambda: fake)
        monkeypatch.setenv("CHECK_QUEUE_URL", "http://local/check")
        monkeypatch.setattr(consumer, "HEARTBEAT_INTERVAL_SECONDS", 3600)

        shutdown = threading.Event()
        poller = threading.Thread(target=run_poller, args=(_QUEUE,), kwargs={"shutdown": shutdown})
        poller.start()

        # Once the first message is mid-flight, request shutdown, then release.
        assert handler.entered.wait(timeout=5)
        shutdown.set()
        handler.release.set()
        poller.join(timeout=5)

        # The first message was finished; the second was never started.
        assert len(handler.calls) == 1
        assert handler.calls[0][0] == {"id": "first"}
        assert fake.deleted_receipts == ["rh-m1"]

    def test_shutdown_during_long_poll_starts_no_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler = RecordingHandler()
        # A message is available, but shutdown is set before the loop runs, so
        # the loop must exit without processing anything.
        fake = FakeSqs([_poller_message("m1", {"id": "never"})])
        monkeypatch.setattr(consumer, "_load_handler", lambda _q: handler)
        monkeypatch.setattr(consumer, "_build_sqs_client", lambda: fake)
        monkeypatch.setenv("CHECK_QUEUE_URL", "http://local/check")

        shutdown = threading.Event()
        shutdown.set()  # already shutting down

        run_poller(_QUEUE, shutdown=shutdown)

        assert handler.calls == []
        assert fake.receive_calls == 0

    def test_shutdown_set_between_receive_and_process_skips_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If shutdown arrives after a message is received but before processing,
        the poller leaves it (does not begin a new message)."""
        handler = RecordingHandler()

        class ShutdownOnReceiveSqs(FakeSqs):
            def __init__(self, messages: list[dict[str, Any]], event: threading.Event) -> None:
                super().__init__(messages)
                self._event = event

            def receive_message(self, **kwargs: Any) -> dict[str, Any]:
                result = super().receive_message(**kwargs)
                # Simulate SIGTERM arriving during the long poll.
                if result.get("Messages"):
                    self._event.set()
                return result

        shutdown = threading.Event()
        fake = ShutdownOnReceiveSqs([_poller_message("m1", {"id": "late"})], shutdown)
        monkeypatch.setattr(consumer, "_load_handler", lambda _q: handler)
        monkeypatch.setattr(consumer, "_build_sqs_client", lambda: fake)
        monkeypatch.setenv("CHECK_QUEUE_URL", "http://local/check")

        run_poller(_QUEUE, shutdown=shutdown)

        # The received message was not processed because shutdown was set.
        assert handler.calls == []
        assert fake.deleted_receipts == []


# ---------------------------------------------------------------------------
# Handler protocol conformance (keeps the test doubles honest / mypy strict)
# ---------------------------------------------------------------------------


def test_recording_handler_satisfies_handler_protocol() -> None:
    handler: Handler = RecordingHandler()
    assert handler.queue == _QUEUE


def test_blocking_handler_satisfies_handler_protocol() -> None:
    handler: Handler = BlockingHandler()
    assert handler.queue == _QUEUE


# ===========================================================================
# Queue-name routing from the eventSourceARN, including the DLQ backstop
# ===========================================================================


class TestQueueNameFromArn:
    """_queue_name_from_arn maps each queue ARN to its logical handler name.

    The DLQ backstop (review-analysis task 1.3) is wired to all three
    dead-letter queues, so every ``-dlq`` resource must route to ``dlq`` and
    NEVER to the primary queue whose name it shares a prefix with.
    """

    @pytest.mark.parametrize(
        ("resource", "expected"),
        [
            ("check-queue", "check"),
            ("processing-queue.fifo", "processing"),
            ("push-queue", "push"),
            # Dead-letter queues all route to the dlq backstop.
            ("check-queue-dlq", "dlq"),
            ("processing-queue-dlq.fifo", "dlq"),
            ("push-queue-dlq", "dlq"),
        ],
    )
    def test_routes_primary_and_dlq_queues(self, resource: str, expected: str) -> None:
        arn = f"arn:aws:sqs:{_REGION}:123456789012:{resource}"
        assert consumer._queue_name_from_arn(arn) == expected

    def test_unknown_queue_raises(self) -> None:
        with pytest.raises(ValueError, match="cannot determine queue name"):
            consumer._queue_name_from_arn("arn:aws:sqs:us-east-1:1:something-else")
