"""Dead-letter queue consumer for the processing queue (review-analysis 1.3).

When a processing message exhausts its retries (SQS ``maxReceiveCount``, 3), SQS
moves it to the processing queue's dead-letter queue instead of deleting it. The
Worker's own final-attempt handling (tasks 6) normally writes a user-safe
``failed`` before that happens, but a message can still reach the DLQ without
that write — for example when the Worker was hard-killed (Lambda timeout, OOM)
on every attempt before it could record the failure. This consumer is the
backstop: it reads the dead-lettered ``{dataset_id, data_version}`` message and
ensures the version ends ``failed`` so the dataset never sits ``processing``
forever (design: "A DLQ consumer does the same ``failed`` transition for
messages that exhaust their retries").

The business logic is shared with the sweeper: :func:`app.jobs.sweep.fail_version`
transitions the dataset to ``failed`` and completes its in-flight
``dataset_versions`` row, guarded so a version the Worker already failed (or that
completed late) is left untouched. That guard is what makes this consumer safe
against the two ways it can see the same work twice: a redelivery of the same
dead-letter message, and a race with the sweeper's stale-``processing`` recovery.

Runtime wiring: this is a plain ``Handler`` with ``handle(body, meta)``. The
DLQ consumer Lambda is wired to **all three** dead-letter queues
(``check-queue-dlq``, ``processing-queue-dlq.fifo``, ``push-queue-dlq``), and
:func:`app.consumer._queue_name_from_arn` routes any ``-dlq`` queue here. Only
the processing DLQ carries a ``{dataset_id, data_version}`` body, so a
dead-lettered check (``{check_id, item_id}``) or push message has no dataset
version to fail: :func:`_parse` returns ``None`` for it and the message is
dropped. The dead-letter queues, redrive policies, and event-source mappings are
provisioned by ``platform-foundation``; nothing here constructs them, and no
Lambda event shape is imported outside the runtime.

Engineering rules honoured: stateless (correctness is the dataset row, not
process memory); same code in both compute modes; idempotent (the shared
``fail_version`` guard tolerates duplicate and concurrent delivery); every
status change goes through :func:`app.db.status.transition` (inside
``fail_version``).
"""

from __future__ import annotations

import logging
from typing import Any

from app.consumer import MessageMeta
from app.jobs.sweep import fail_version

logger = logging.getLogger(__name__)

#: User-safe message recorded on a version that reached the DLQ (Requirement
#: 7.2: "an error message that is safe to show users"). The pipeline writes a
#: more specific message when it can detect the final attempt itself; this is
#: the fallback for a message that was dead-lettered without that write.
DLQ_FAILURE_MESSAGE = "Processing failed after multiple attempts. Please try refreshing."


def _parse(body: dict[str, Any]) -> tuple[str, int] | None:
    """Extract ``(dataset_id, version)`` from a dead-lettered processing body.

    The processing message carries IDs only; the version is enqueued under
    ``data_version`` by the Add, Refresh, and sweeper producers (``version`` is
    accepted as a synonym for forward compatibility). Returns ``None`` for a
    body that is missing or has a malformed id/version so the caller can drop it.
    """
    dataset_id = body.get("dataset_id")
    version = body.get("data_version", body.get("version"))

    if not isinstance(dataset_id, str) or not dataset_id:
        return None
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    return dataset_id, version


class DlqHandler:
    """Marks a dead-lettered processing version ``failed`` (Requirement 7.2)."""

    queue: str = "dlq"

    def handle(self, body: dict[str, Any], meta: MessageMeta) -> None:
        # The dead-lettered body is the original processing message. A malformed
        # body here is a producer error with nowhere left to retry — log and
        # drop it so the DLQ drains rather than looping (there is no second DLQ
        # behind this one).
        parsed = _parse(body)
        if parsed is None:
            logger.error("dlq: malformed dead-letter body dropped: %r", body)
            return

        dataset_id, version = parsed
        failed = fail_version(dataset_id, version, DLQ_FAILURE_MESSAGE)
        if failed:
            logger.info(
                "dlq: marked dataset %s v%d failed after exhausting retries",
                dataset_id,
                version,
            )
        else:
            logger.info(
                "dlq: dataset %s v%d already resolved; nothing to do",
                dataset_id,
                version,
            )


handler = DlqHandler()
