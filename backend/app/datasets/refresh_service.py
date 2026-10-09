"""Refresh Service: re-capture and re-process an existing dataset as a new version.

This module is **shared** between ``dataset-ingestion`` (adding an
already-tracked URL, Requirement 6.4–6.8) and ``dataset-library`` (the Refresh
action). A refresh always starts from a *capture* the caller already produced —
a fresh Check for a URL dataset or an upload for an upload dataset — so the new
data version has concrete artifacts to copy. The single public entry point is
:func:`refresh`.

Behaviour (design "Backend modules" → ``datasets.refresh_service.refresh``):

- If the dataset is ``requested`` or ``processing``, a refresh is already in
  flight: return :data:`ALREADY_REFRESHING` and do nothing else (Requirement
  6.6).
- If the dataset is archived, clear ``archived_at`` and refresh it, returning
  :data:`RESTORED_AND_REFRESHED` (Requirement 6.5).
- Otherwise increment ``data_version``, copy the capture (page + plan + optional
  snapshot for URLs; file + mapping for uploads) into ``raw/v{n}`` using
  :mod:`app.storage.keys`, insert a ``dataset_versions`` row with the given
  ``trigger``, transition the dataset to ``requested`` with a
  ``refresh_requested`` event (Requirement 6.8), and enqueue a processing
  message ``{dataset_id, data_version}`` onto the FIFO processing queue. The
  version increment, status flip to ``requested`` and version-row insert happen
  in **one** transaction guarded by ``WHERE status NOT IN
  ('requested','processing')``; because that same guarded write moves the row
  into the in-flight range, any number of concurrent refreshes produce
  **exactly one** new version (Correctness Property 7, Requirement 6.6).

Engineering rules honoured:

- **Stateless.** No correctness depends on process memory; shared state is in
  Aurora, S3, and SQS.
- **Idempotent / concurrency-safe.** The conditional ``UPDATE`` guard means two
  concurrent refreshes race on the same row; the loser's guarded update affects
  zero rows and it reports ``already_refreshing`` rather than creating a second
  version.
- **DB only through ``core.db``**; every status change through
  :func:`app.db.status.transition`; every S3 key from :mod:`app.storage.keys`;
  queue bodies carry IDs only and are sent through :func:`app.core.queue.enqueue`
  to the FIFO processing queue with ``message_group_id = dataset_id``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.queue import enqueue
from app.db.models import DatasetStatus
from app.db.status import transition
from app.storage import keys, s3

logger = logging.getLogger(__name__)

#: Trigger literals stored on the ``dataset_versions`` row (design Data Models).
RefreshTrigger = Literal["manual_refresh", "duplicate_submission", "upload_replace"]

#: Outcomes returned to the caller (a subset of the Add endpoint's ``outcome``).
RefreshOutcome = Literal["refreshed", "restored_and_refreshed", "already_refreshing"]

ALREADY_REFRESHING: RefreshOutcome = "already_refreshing"
RESTORED_AND_REFRESHED: RefreshOutcome = "restored_and_refreshed"
REFRESHED: RefreshOutcome = "refreshed"

#: Statuses that mean a refresh is already in flight (Requirement 6.6).
_IN_FLIGHT = (DatasetStatus.REQUESTED.value, DatasetStatus.PROCESSING.value)


# ---------------------------------------------------------------------------
# Capture value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckCapture:
    """A URL Check's temporary capture, ready to become a dataset version.

    The caller (Add for an already-tracked URL, or the Library Refresh action)
    produces this from a completed Check item. It names the S3 objects the
    Refresh Service copies into the dataset's ``raw/v{n}`` location:

    Attributes:
        page_key: S3 key of the rendered HTML (``checks/{id}/{item}/page.html``,
            from :func:`app.storage.keys.check_page`). Copied to
            ``raw/v{n}/page-1.html``.
        plan_key: S3 key of the Extraction Plan
            (``checks/{id}/{item}/plan.json``). Copied to ``raw/v{n}/plan.json``.
        snapshot_key: Optional S3 key of the above-the-fold screenshot
            (``checks/{id}/{item}/snapshot.png``). Copied to
            ``snapshot/v{n}.png`` when present.
    """

    page_key: str
    plan_key: str
    snapshot_key: str | None = None


@dataclass(frozen=True)
class UploadCapture:
    """An upload's staged file and mapping, ready to become a dataset version.

    The caller produces this from a validated upload. It names the S3 objects
    the Refresh Service copies into the dataset's ``raw/v{n}`` location:

    Attributes:
        file_key: S3 key of the uploaded tabular file (``uploads/{id}/file``,
            from :func:`app.storage.keys.upload_file`). Copied to
            ``raw/v{n}/upload.csv``.
        mapping_key: S3 key of the confirmed column mapping JSON. Copied to
            ``raw/v{n}/mapping.json``.
    """

    file_key: str
    mapping_key: str


Capture = CheckCapture | UploadCapture


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def refresh(
    dataset_id: str,
    trigger: RefreshTrigger,
    capture: Capture,
) -> RefreshOutcome:
    """Refresh an existing dataset as a new data version from *capture*.

    A capture is always required: a refresh re-processes concrete artifacts the
    caller produced through a Check (``CheckCapture``) or an upload
    (``UploadCapture``). See the module docstring for the full behaviour.

    Args:
        dataset_id: The existing dataset to refresh.
        trigger: What started this refresh — ``manual_refresh`` (Library),
            ``duplicate_submission`` (adding an already-tracked URL), or
            ``upload_replace`` (re-uploading). Stored on the new
            ``dataset_versions`` row.
        capture: The artifacts to copy into the new version
            (:class:`CheckCapture` for URL datasets, :class:`UploadCapture` for
            uploads).

    Returns:
        - ``already_refreshing`` when the dataset is already ``requested`` or
          ``processing`` (nothing is changed).
        - ``restored_and_refreshed`` when the dataset was archived (it is
          restored and a new version is started).
        - ``refreshed`` when a new version is started on an idle dataset.

    Raises:
        ValueError: If the dataset does not exist.
    """
    # One transaction performs the guarded version increment + row insert so
    # concurrent refreshes produce exactly one new version (Property 7). It
    # returns the new version number and whether the dataset was archived; a
    # None result means the guard matched nothing — a refresh is in flight.
    result = _claim_new_version(dataset_id, trigger)
    if result is None:
        logger.info("dataset %s already refreshing; no new version", dataset_id)
        return ALREADY_REFRESHING

    new_version, was_archived = result

    # Copy the capture artifacts into the dataset's permanent raw/v{n} location.
    # Done after the row is claimed so the version number is known; done before
    # enqueue so processing never dequeues a version whose artifacts are missing.
    _copy_capture(dataset_id, new_version, capture)

    # Record the refresh as a status change (Requirement 6.8): a
    # `refresh_requested` event carrying the trigger, through db.status so the
    # event log and status column move together and an event is published.
    transition(
        dataset_id,
        DatasetStatus.REQUESTED,
        "refresh_requested",
        extra={"trigger": trigger, "version": new_version},
    )

    # Hand the new version to review-analysis. Queue body carries IDs only; the
    # processing queue is FIFO keyed by dataset_id so a dataset's versions are
    # processed in order and dedup is content-based per group.
    enqueue(
        get_settings().processing_queue_url,
        {"dataset_id": dataset_id, "data_version": new_version},
        message_group_id=dataset_id,
        # FIFO queue has content-based dedup OFF (api-stack.ts): the producer
        # must supply the dedup id, keyed dataset_id:version.
        message_deduplication_id=f"{dataset_id}:{new_version}",
    )

    return RESTORED_AND_REFRESHED if was_archived else REFRESHED


# ---------------------------------------------------------------------------
# Capture copy (every key from storage.keys)
# ---------------------------------------------------------------------------


def _copy_capture(dataset_id: str, version: int, capture: Capture) -> None:
    """Copy *capture*'s artifacts into the dataset's permanent ``raw/v{n}``.

    For a :class:`CheckCapture` (URL dataset): the rendered page becomes
    ``raw/v{n}/page-1.html``, the plan becomes ``raw/v{n}/plan.json``, and the
    screenshot (when present) becomes ``snapshot/v{n}.png``. For an
    :class:`UploadCapture`: the file becomes ``raw/v{n}/upload.csv`` and the
    mapping becomes ``raw/v{n}/mapping.json``. Every destination key comes from
    :mod:`app.storage.keys`.
    """
    if isinstance(capture, CheckCapture):
        s3.copy_object(capture.page_key, keys.dataset_raw_page(dataset_id, version, 1))
        s3.copy_object(capture.plan_key, keys.dataset_raw_plan(dataset_id, version))
        if capture.snapshot_key is not None and s3.object_exists(capture.snapshot_key):
            s3.copy_object(capture.snapshot_key, keys.dataset_snapshot(dataset_id, version))
    else:
        s3.copy_object(capture.file_key, keys.dataset_raw_upload(dataset_id, version))
        s3.copy_object(capture.mapping_key, keys.dataset_raw_mapping(dataset_id, version))


# ---------------------------------------------------------------------------
# Transactional version claim (the concurrency guard)
# ---------------------------------------------------------------------------


def _claim_new_version(dataset_id: str, trigger: RefreshTrigger) -> tuple[int, bool] | None:
    """Atomically increment ``data_version`` and insert the version row.

    In **one** transaction this:

    1. Increments ``data_version`` on the dataset **and sets its status to
       ``requested``** **only when** its status is not already ``requested`` or
       ``processing``, clearing ``archived_at`` at the same time
       (restore-on-refresh, Requirement 6.5). Flipping the status into the
       in-flight range inside this guarded, row-locked write is what makes
       concurrent refreshes safe: exactly one caller's ``UPDATE`` matches the
       row (bumping the version and marking it ``requested``); every other
       caller then finds ``status IN ('requested','processing')`` and its
       ``UPDATE`` matches nothing (Property 7).
    2. Inserts the matching ``dataset_versions`` row with ``trigger`` so the
       version increment and its row never diverge even under concurrency.

    The row is locked with ``SELECT ... FOR UPDATE`` so refreshers serialise:
    the first to acquire the lock reads a non-in-flight status, and its guarded
    ``UPDATE`` both bumps the version and marks the row ``requested`` before the
    transaction commits and the lock is released. The next waiter then sees the
    committed ``requested`` status, fails the guard, and returns ``None``. The
    ``status = 'requested'`` write is the real guarantee here; the
    ``SELECT ... FOR UPDATE`` and the ``_IN_FLIGHT`` early-return are kept as
    belt-and-braces. The ``refresh_requested`` event is appended and published
    exactly once by the single ``db.status.transition`` call back in
    :func:`refresh` (setting the column to ``requested`` here is idempotent with
    that later transition — one event, one publish per new version). This yields
    exactly one new version (Property 7).

    Returns:
        ``(new_version, was_archived)`` when this call won the guard, or
        ``None`` when the dataset was already refreshing (guard matched no row).

    Raises:
        ValueError: If the dataset does not exist at all.
    """
    with session_scope() as session:
        # Lock the row so concurrent refreshers serialise here. Reading the
        # current status + archived flag under the lock lets us both apply the
        # "already refreshing" guard and report restore-on-refresh correctly.
        row = session.execute(
            text(
                "SELECT status, (archived_at IS NOT NULL) AS was_archived "
                "FROM datasets WHERE id = CAST(:id AS uuid) FOR UPDATE"
            ),
            {"id": dataset_id},
        ).first()

        if row is None:
            raise ValueError(f"Dataset {dataset_id!r} does not exist")

        status = str(row[0])
        was_archived = bool(row[1])

        # Already refreshing: the guard excludes this dataset. The loser of a
        # concurrent race arrives here after the winner's guarded UPDATE
        # committed `status = 'requested'`, so it reports already_refreshing
        # instead of creating a second version (Property 7, Requirement 6.6).
        if status in _IN_FLIGHT:
            return None

        # Guarded increment (restore-on-refresh clears archived_at). The write
        # ALSO sets ``status = 'requested'`` in the SAME row-locked, guarded
        # statement: this is what makes concurrent refreshes safe (Property 7).
        # By flipping the status into the in-flight range inside the winning
        # write, a second racer's ``status NOT IN ('requested','processing')``
        # guard matches no row, so its ``RETURNING`` is empty and this call
        # returns ``None`` -> the caller reports ``already_refreshing``. (The
        # ``SELECT ... FOR UPDATE`` + ``_IN_FLIGHT`` early-return above remain as
        # belt-and-braces; the real guarantee is this status flip.) The
        # ``refresh_requested`` event itself is still appended + published by the
        # single ``db.status.transition`` call back in ``refresh()`` so there is
        # exactly one event and one publish per new version; setting the column
        # to ``requested`` here is idempotent with that later transition.
        #
        # NOTE (cross-spec): the ``- 'refresh_check_id'`` key-drop below is a
        # *dataset-library* concern piggy-backing on this dataset-ingestion
        # atomic version write; it clears the Library marker for every
        # new-version path in one race-free write (see task-3 cleanup).
        #
        # Clearing ``status_detail.refresh_check_id`` here is what makes the
        # Library's "Checking page…" / "Needs confirmation" marker disappear
        # once a refresh Check *resolves into a new version* (dataset-library
        # task 3). The id is recorded by ``POST /datasets/{id}/refresh`` while
        # the Check runs and surfaced by the list/detail ``refresh_check_id``
        # field; a refresh (whether started automatically by the check handler,
        # by the confirm endpoint, by the upload-replace endpoint, or by a
        # duplicate submission) means that Check is done, so we drop the key in
        # the same guarded write that bumps the version. ``- 'refresh_check_id'``
        # is a no-op when the key is absent (e.g. a duplicate-submission refresh
        # that never set one), so it is always safe.
        new_version_row = session.execute(
            text(
                """
                UPDATE datasets
                SET data_version = data_version + 1,
                    status = 'requested',
                    archived_at = NULL,
                    status_detail = status_detail - 'refresh_check_id'
                WHERE id = CAST(:id AS uuid)
                  AND status NOT IN ('requested', 'processing')
                RETURNING data_version
                """
            ),
            {"id": dataset_id},
        ).first()
        # Under the row lock the guard cannot have changed since the SELECT.
        assert new_version_row is not None
        new_version = int(new_version_row[0])

        # Insert the matching version row in the SAME transaction as the
        # increment (design: "The version increment and row insert happen in
        # one transaction"), so a committed increment always has its row.
        session.execute(
            text(
                """
                INSERT INTO dataset_versions (dataset_id, version, trigger, requested_at)
                VALUES (CAST(:id AS uuid), :version, :trigger, now())
                """
            ),
            {"id": dataset_id, "version": new_version, "trigger": trigger},
        )

        return new_version, was_archived
