"""Queue consumer runtime.

One code path, two compute modes. Background work is always a ``Handler`` with
``handle(body, meta)``; this module runs that handler either from an SQS event
(Lambda) or from a long-poll loop (container).

Provides:
- the ``Handler`` protocol (implemented by each queue handler module)
- ``MessageMeta`` carrying the receive count, so handlers detect the final
  attempt the same way in both modes
- ``lambda_entry`` – the Lambda SQS adapter, reporting partial batch failures
- ``run_poller`` – the container long-poll loop with a visibility heartbeat and
  graceful ``SIGTERM`` shutdown

Usage (container mode)::

    python -m app.consumer --queue check
    python -m app.consumer --queue processing
    python -m app.consumer --queue push

Lambda mode: the SQS event-source mapping invokes ``lambda_entry`` (aliased as
``lambda_handler``) directly.

Lambda-specific event shapes are confined to this module; nothing outside
``app.consumer`` imports or inspects them.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import signal
import threading
from dataclasses import dataclass
from types import FrameType
from typing import Any, Protocol

import boto3

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageMeta:
    """Metadata attached to each message, identical in both compute modes.

    ``receive_count`` is SQS's ``ApproximateReceiveCount`` (1 on first
    delivery). Handlers use it to detect the final attempt so they record a
    user-safe failure before the message moves to the DLQ.
    """

    message_id: str
    receive_count: int

    @property
    def is_final_attempt(self) -> bool:
        """True on the last delivery before the message goes to the DLQ."""
        return self.receive_count >= MAX_RECEIVE_COUNT


class Handler(Protocol):
    """Protocol that every queue handler must implement."""

    queue: str  # "check" | "processing" | "push"

    def handle(self, body: dict[str, Any], meta: MessageMeta) -> None:
        """Process a single message idempotently."""
        ...


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# SQS redrive maxReceiveCount: after this many deliveries the message is moved
# to the DLQ. Handlers treat the last delivery as the final attempt.
MAX_RECEIVE_COUNT = 3

# Container poller timings.
LONG_POLL_WAIT_SECONDS = 20
HEARTBEAT_INTERVAL_SECONDS = 30
# The visibility window granted on each heartbeat; must exceed the heartbeat
# interval so a message never becomes visible again while a handler is working.
VISIBILITY_EXTENSION_SECONDS = 90


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

# Maps queue name -> dotted module path that exports a ``handler`` instance.
_HANDLER_MODULES: dict[str, str] = {
    "check": "app.handlers.check",
    "processing": "app.handlers.processing",
    "push": "app.handlers.push",
    # The dead-letter backstop (review-analysis task 1.3). The DLQ consumer
    # Lambda is wired to all three dead-letter queues (check/processing/push);
    # every dead-lettered message routes here so the affected dataset version is
    # marked `failed` rather than re-running the original handler.
    "dlq": "app.handlers.dlq",
}

# Maps queue name -> environment variable holding the queue URL.
_QUEUE_URL_ENV: dict[str, str] = {
    "check": "CHECK_QUEUE_URL",
    "processing": "PROCESSING_QUEUE_URL",
    "push": "PUSH_QUEUE_URL",
    "dlq": "DLQ_QUEUE_URL",
}


def _load_handler(queue: str) -> Handler:
    """Import and return the handler instance for the given queue name."""
    module_path = _HANDLER_MODULES.get(queue)
    if module_path is None:
        raise ValueError(f"Unknown queue name: {queue!r}")
    module = importlib.import_module(module_path)
    handler: Handler = module.handler
    return handler


def _get_queue_url(queue: str) -> str:
    env_var = _QUEUE_URL_ENV.get(queue)
    if env_var is None:
        raise ValueError(f"Unknown queue name: {queue!r}")
    url = os.environ.get(env_var)
    if not url:
        raise RuntimeError(f"Environment variable {env_var} is not set")
    return url


# ---------------------------------------------------------------------------
# Lambda SQS adapter
# ---------------------------------------------------------------------------


def lambda_entry(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:  # noqa: ANN401
    """AWS Lambda entry point for SQS event-source mappings.

    Processes every record in the batch and reports the IDs of any that failed
    through ``batchItemFailures``. With partial batch responses enabled on the
    event-source mapping, SQS then retries only the failed messages and the
    successful ones are deleted.

    The logical queue name is derived from each record's ``eventSourceARN`` so a
    single function can serve any queue, and the shared ``handle`` code runs
    unchanged.
    """
    failed: list[dict[str, str]] = []

    records: list[dict[str, Any]] = event.get("Records", [])
    if not records:
        return {"batchItemFailures": failed}

    # All records in one SQS batch come from the same queue.
    source_arn: str = records[0].get("eventSourceARN", "")
    queue = _queue_name_from_arn(source_arn)
    handler = _load_handler(queue)

    for record in records:
        message_id: str = record["messageId"]
        receive_count = _receive_count(record.get("attributes", {}))
        meta = MessageMeta(message_id=message_id, receive_count=receive_count)
        try:
            body: dict[str, Any] = json.loads(record["body"])
            handler.handle(body, meta)
        except Exception:
            logger.exception(
                "handler failed for message %s on %s (attempt %d/%d)",
                message_id,
                queue,
                receive_count,
                MAX_RECEIVE_COUNT,
            )
            failed.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failed}


# AWS Lambda defaults to a handler named ``lambda_handler``; keep an alias so
# the function configuration can reference either name.
lambda_handler = lambda_entry


def _receive_count(attributes: dict[str, Any]) -> int:
    """Read ``ApproximateReceiveCount`` from SQS attributes, defaulting to 1."""
    return int(attributes.get("ApproximateReceiveCount", "1"))


def _queue_name_from_arn(source_arn: str) -> str:
    """Map an SQS ``eventSourceARN`` to a logical queue name.

    The ARN's last segment is the queue resource name (for example
    ``check-queue`` or ``processing-queue.fifo``).

    Any dead-letter queue (resource name containing ``-dlq``, e.g.
    ``check-queue-dlq`` or ``processing-queue-dlq.fifo``) routes to the ``dlq``
    backstop handler regardless of which primary queue it belongs to — a
    dead-lettered message has exhausted its retries and must not be re-run
    through the original handler. This check comes first so a DLQ name is never
    mistaken for its primary queue by the prefix match below.
    """
    resource = source_arn.split(":")[-1]
    if "-dlq" in resource:
        return "dlq"
    for name in _HANDLER_MODULES:
        if name == "dlq":
            continue
        if resource.startswith(f"{name}-") or resource == name:
            return name
    raise ValueError(f"cannot determine queue name from ARN: {source_arn!r}")


# ---------------------------------------------------------------------------
# Container long-poll loop
# ---------------------------------------------------------------------------


def _build_sqs_client() -> Any:  # noqa: ANN401 - boto3 client type is dynamic
    endpoint_url = os.environ.get("AWS_ENDPOINT_URL")
    return boto3.client("sqs", endpoint_url=endpoint_url or None)


def _run_heartbeat(
    sqs: Any,  # noqa: ANN401
    queue_url: str,
    receipt_handle: str,
    message_id: str,
    stop: threading.Event,
) -> None:
    """Extend a message's visibility every heartbeat interval until stopped.

    Runs in a background thread while the handler works. The ``stop`` event is
    waited on (not slept through) so the thread exits promptly once the handler
    finishes or shutdown begins.
    """
    while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
        try:
            sqs.change_message_visibility(
                QueueUrl=queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=VISIBILITY_EXTENSION_SECONDS,
            )
            logger.debug("heartbeat: extended visibility for %s", message_id)
        except Exception:
            # A failed extension is not fatal: worst case the message becomes
            # visible again and is retried idempotently by another instance.
            logger.warning("heartbeat: failed to extend visibility for %s", message_id)


def run_poller(queue: str, shutdown: threading.Event | None = None) -> None:
    """Long-poll an SQS queue and run the handler for each message.

    - Long-polls for up to ``LONG_POLL_WAIT_SECONDS`` seconds per receive.
    - Extends message visibility every ``HEARTBEAT_INTERVAL_SECONDS`` seconds
      while the handler is working.
    - Deletes the message on success; leaves it in the queue on failure so SQS
      retries it and eventually moves it to the DLQ.
    - On ``SIGTERM`` it stops taking new messages and finishes the current one
      before returning, so ECS can stop the task within its stop timeout.

    ``shutdown`` may be supplied by callers (and tests) to drive the loop; when
    omitted a fresh event is created and wired to ``SIGTERM``.
    """
    queue_url = _get_queue_url(queue)
    handler = _load_handler(queue)
    sqs = _build_sqs_client()

    stop = shutdown if shutdown is not None else _install_sigterm_handler()

    logger.info("poller started for queue %r (%s)", queue, queue_url)

    while not stop.is_set():
        response = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=LONG_POLL_WAIT_SECONDS,
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = response.get("Messages", [])
        if not messages:
            continue

        # SIGTERM may have arrived during the long poll; if so, do not begin a
        # new message. It stays invisible until its visibility timeout lapses
        # and is redelivered to another instance.
        if stop.is_set():
            break

        _process_one(sqs, queue, queue_url, handler, messages[0])

    logger.info("poller for queue %r shut down cleanly", queue)


def _process_one(
    sqs: Any,  # noqa: ANN401
    queue: str,
    queue_url: str,
    handler: Handler,
    message: dict[str, Any],
) -> None:
    """Run the handler for a single received message, with a visibility heartbeat."""
    message_id: str = message["MessageId"]
    receipt_handle: str = message["ReceiptHandle"]
    receive_count = _receive_count(message.get("Attributes", {}))
    meta = MessageMeta(message_id=message_id, receive_count=receive_count)

    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_run_heartbeat,
        args=(sqs, queue_url, receipt_handle, message_id, heartbeat_stop),
        name=f"heartbeat-{message_id[:8]}",
        daemon=True,
    )
    heartbeat.start()

    try:
        body: dict[str, Any] = json.loads(message["Body"])
        handler.handle(body, meta)
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        logger.debug("message %s on %s processed and deleted", message_id, queue)
    except Exception:
        logger.exception(
            "handler failed for message %s on %s (attempt %d/%d); leaving in queue",
            message_id,
            queue,
            receive_count,
            MAX_RECEIVE_COUNT,
        )
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=5)


def _install_sigterm_handler() -> threading.Event:
    """Create a shutdown event set when ``SIGTERM`` is received."""
    stop = threading.Event()

    def _on_sigterm(signum: int, frame: FrameType | None) -> None:
        logger.info("SIGTERM received; finishing current message then exiting")
        stop.set()

    signal.signal(signal.SIGTERM, _on_sigterm)
    return stop


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _main() -> None:
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="ReviewLens queue consumer")
    parser.add_argument(
        "--queue",
        required=True,
        choices=list(_HANDLER_MODULES),
        help="name of the queue to consume",
    )
    args = parser.parse_args()

    # In container mode (not on Lambda) expose health/readiness for the
    # orchestrator to probe.
    if not os.environ.get("LAMBDA_TASK_ROOT"):
        from app.core.health import start_consumer_health_listener

        start_consumer_health_listener(int(os.environ.get("HEALTH_PORT", "8080")))

    run_poller(args.queue)


if __name__ == "__main__":
    _main()
