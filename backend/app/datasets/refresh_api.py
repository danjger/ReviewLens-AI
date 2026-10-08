"""Refresh API domain logic (dataset-library task 3).

The Library's Refresh action and its two follow-ups live here as pure domain
functions; :mod:`app.datasets.api` is a thin controller over them. They
implement the three refresh endpoints from the design's "API endpoints" table
(Requirement 4):

- :func:`start_refresh` — ``POST /datasets/{id}/refresh``. Starts a refresh of a
  URL dataset's original URL by creating a one-item **refresh-origin** Check
  Session, enqueuing its item onto ``check-queue`` (IDs only), and recording the
  running Check's id on ``status_detail.refresh_check_id`` so the list/detail
  ``refresh_check_id`` field (task 1) surfaces "Checking page…". Returns the new
  ``check_id``. The check handler (dataset-ingestion task 6.2) then runs the
  same probe → capture → assess pipeline and, from the fresh verdict, either
  auto-refreshes through :mod:`app.datasets.refresh_service`, leaves the item
  ``awaiting_confirmation`` (a ``limited`` verdict for a previously ``will_work``
  dataset), or appends a failed-refresh event with no new version (Requirements
  4.1, 4.2). A ``409 ALREADY_REFRESHING`` is raised when a refresh is already in
  flight — the dataset is ``requested``/``processing`` or already has a refresh
  Check recorded (Requirement 4.4).
- :func:`confirm_refresh` — ``POST /datasets/{id}/refresh/confirm``. The analyst
  confirms a ``limited`` verdict that the check handler left
  ``awaiting_confirmation`` (Requirement 4.3). This proceeds with the refresh
  the analyst confirmed, re-using the stored Check's capture and
  ``trigger="manual_refresh"``.
- :func:`refresh_from_upload` — ``POST /datasets/{id}/refresh/upload``. Refresh
  an upload dataset by replacing its data from a previously-staged, previewed
  upload, through the shared Refresh Service with ``trigger="upload_replace"``
  (Requirement 4.5).

Why ``refresh_check_id`` lives on ``status_detail``
---------------------------------------------------
The Library row has to show "Checking page…" / "Needs confirmation" while a
refresh Check is outstanding, but the Check store (DynamoDB ``check-sessions``)
has no index by dataset id, so the row can't find its Check from there. Instead
the running Check's id is written onto the dataset's ``status_detail`` here when
the refresh starts, read back by :func:`app.datasets.library._refresh_check_id`,
and cleared when the Check resolves into a new version by
:func:`app.datasets.refresh_service.refresh` (which drops the key in the same
guarded write that bumps the version). The confirm and upload paths both go
through the Refresh Service, so they clear it too.

Engineering rules honoured:

- **Stateless / DB only through ``core.db``.** ``status_detail`` is read and
  written through :func:`app.core.db.session_scope`; the id write is a guarded,
  concurrency-safe JSONB merge.
- **Idempotent.** Starting a refresh while one is in flight is refused with
  ``409`` rather than creating a second Check; the Refresh Service's own guard
  means a racing automatic refresh still yields exactly one new version.
- **Queue bodies carry IDs only** (``{check_id, item_id}``), enqueued through
  :func:`app.core.queue.enqueue` to ``check_queue_url`` — the same pattern as
  ``create_check`` in :mod:`app.ingestion.api`.
- **Every S3 key comes from** :mod:`app.storage.keys`; **every status change**
  goes through :mod:`app.db.status` (here, indirectly, via the Refresh Service).
- The refresh-origin **Check Session** is created with
  :func:`app.ingestion.check_session.put_session` carrying the target dataset
  id; the handler's existing ``_handle_refresh_origin`` reads it.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import CursorResult

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.errors import AppValidationError, ConflictError, NotFoundError
from app.core.queue import enqueue
from app.datasets import refresh_service
from app.datasets.refresh_service import CheckCapture, RefreshOutcome, UploadCapture
from app.db.models import Dataset, DatasetStatus, SourceType
from app.ingestion import check_session, upload_parser
from app.ingestion.check_session import CheckItem
from app.storage import keys, s3

logger = logging.getLogger(__name__)

#: The single refresh-origin Check item id. A refresh always checks exactly one
#: URL (the dataset's original URL), so a fixed id keeps the session shape and
#: the handler's ``_handle_refresh_origin`` lookup simple.
REFRESH_ITEM_ID = "u1"

#: Error code for a refused refresh while one is already in flight (design Error
#: Handling: "409 ``ALREADY_REFRESHING``").
ALREADY_REFRESHING_CODE = "ALREADY_REFRESHING"

#: Statuses that mean a refresh is already in flight (Requirement 4.4). Mirrors
#: the Refresh Service's own in-flight set.
_IN_FLIGHT = (DatasetStatus.REQUESTED.value, DatasetStatus.PROCESSING.value)

#: Check item states that mean a refresh Check is still outstanding — running or
#: waiting for the analyst to confirm — so a new refresh must be refused and the
#: confirm path has something to act on.
_OUTSTANDING_ITEM_STATES = ("pending", "checking", "awaiting_confirmation")


@dataclass(frozen=True)
class _RefreshTarget:
    """A dataset's fields needed to start/confirm/upload-refresh it."""

    id: str
    source_type: str
    original_url: str | None
    status: str
    refresh_check_id: str | None


# ---------------------------------------------------------------------------
# POST /datasets/{id}/refresh  (Requirements 4.1, 4.2, 4.4, 4.7)
# ---------------------------------------------------------------------------


def start_refresh(dataset_id: str) -> str:
    """Start a refresh of a URL dataset and return the new Check id.

    Creates a one-item refresh-origin Check Session for the dataset's original
    URL, enqueues its item onto ``check-queue`` (IDs only), and records the
    running Check id on ``status_detail.refresh_check_id`` so the Library row can
    show "Checking page…". The check handler then runs the viability pipeline and
    acts on the verdict (auto-refresh, await confirmation, or record a failed
    refresh) — see the module docstring.

    Raises:
        NotFoundError: No dataset has that id (``404``).
        AppValidationError: The dataset is an upload dataset (``422`` — upload
            datasets refresh via :func:`refresh_from_upload`, Requirement 4.5),
            or has no original URL to re-check.
        ConflictError: A refresh is already in flight — the dataset is
            ``requested``/``processing`` or already has a refresh Check recorded
            (``409 ALREADY_REFRESHING``, Requirement 4.4).
    """
    target = _load_target(dataset_id)

    if target.source_type == SourceType.UPLOAD.value:
        raise AppValidationError(
            "This is an uploaded dataset; refresh it by uploading a replacement file."
        )
    if not target.original_url:
        raise AppValidationError("This dataset has no source URL to refresh.")

    _guard_not_refreshing(target)

    # Create the refresh-origin Check Session (one pending item for the original
    # URL) carrying the dataset id so the handler's _handle_refresh_origin can
    # find it. The id is a fresh UUID like a normal Check.
    check_id = str(uuid.uuid4())
    item = CheckItem(
        item_id=REFRESH_ITEM_ID,
        input=target.original_url,
        state="pending",
        normalized=target.original_url,
    )
    check_session.put_session(
        check_id,
        "refresh",
        [item],
        refresh_dataset_id=dataset_id,
    )

    # Record the running Check id BEFORE enqueueing so the row shows
    # "Checking page…" even if the handler picks the item up immediately. The
    # write is guarded so a concurrent start can't clobber another's id.
    _set_refresh_check_id(dataset_id, check_id)

    # Enqueue the single item (IDs only) — same pattern as create_check.
    enqueue(
        get_settings().check_queue_url,
        {"check_id": check_id, "item_id": REFRESH_ITEM_ID},
    )

    return check_id


# ---------------------------------------------------------------------------
# POST /datasets/{id}/refresh/confirm  (Requirement 4.3)
# ---------------------------------------------------------------------------


def confirm_refresh(dataset_id: str, check_id: str) -> RefreshOutcome:
    """Confirm a ``limited`` refresh verdict and proceed with the refresh.

    The check handler left the refresh Check's single item
    ``awaiting_confirmation`` because the fresh verdict was ``limited`` for a
    dataset that was previously ``will_work`` (Requirement 4.3). This proceeds
    with the refresh the analyst confirmed: it re-uses that Check's capture (the
    rendered page + plan already stored under the check prefix) and calls the
    shared Refresh Service with ``trigger="manual_refresh"`` — the same record a
    manual refresh produces.

    Args:
        dataset_id: The dataset being refreshed.
        check_id: The refresh Check whose ``limited`` verdict is being confirmed.

    Returns:
        The Refresh Service outcome (``refreshed`` / ``restored_and_refreshed`` /
        ``already_refreshing``).

    Raises:
        NotFoundError: No dataset has that id, or the Check Session has expired
            or does not belong to this dataset, or its item is not awaiting
            confirmation (``404``).
    """
    target = _load_target(dataset_id)  # 404 if the dataset is gone

    session = check_session.get_session(check_id)
    if session is None or session.refresh_dataset_id != dataset_id:
        raise NotFoundError("Refresh check not found or expired")

    item = session.items.get(REFRESH_ITEM_ID)
    if item is None or item.state != "awaiting_confirmation":
        raise NotFoundError("This refresh is not waiting for confirmation")

    capture = CheckCapture(
        page_key=keys.check_page(check_id, REFRESH_ITEM_ID),
        plan_key=keys.check_plan(check_id, REFRESH_ITEM_ID),
        snapshot_key=keys.check_snapshot(check_id, REFRESH_ITEM_ID),
    )
    outcome = refresh_service.refresh(
        target.id,
        trigger="manual_refresh",
        capture=capture,
    )

    # Mark the Check item applied so a repeated confirm is a no-op and the row
    # stops showing "Needs confirmation". The Refresh Service already cleared
    # status_detail.refresh_check_id in its guarded write.
    check_session.set_item_fields(
        check_id,
        REFRESH_ITEM_ID,
        {"state": "applied"},
        expected_state="awaiting_confirmation",
    )
    return outcome


# ---------------------------------------------------------------------------
# POST /datasets/{id}/refresh/upload  (Requirement 4.5)
# ---------------------------------------------------------------------------


def refresh_from_upload(
    dataset_id: str,
    upload_id: str,
    mapping: dict[str, str],
) -> RefreshOutcome:
    """Refresh an upload dataset by replacing its data from a staged upload.

    Re-validates the staged file (re-parsing it through
    :func:`app.ingestion.upload_parser.preview_upload`, which deletes the object
    and raises on an invalid file), stages the confirmed column mapping next to
    the upload (``uploads/{id}/mapping.json`` via :func:`app.storage.keys`), and
    calls the shared Refresh Service with ``trigger="upload_replace"`` and an
    :class:`~app.datasets.refresh_service.UploadCapture`, so the new data version
    is processed exactly like an initial upload (Requirement 4.5).

    Args:
        dataset_id: The upload dataset to refresh.
        upload_id: The staged upload's id (from ``POST /uploads``).
        mapping: The analyst-confirmed canonical-field → header-name mapping.

    Returns:
        The Refresh Service outcome (``refreshed`` / ``restored_and_refreshed`` /
        ``already_refreshing``).

    Raises:
        NotFoundError: No dataset has that id (``404``).
        AppValidationError: The dataset is a URL dataset (``422`` — URL datasets
            refresh via :func:`start_refresh`), or the staged file is invalid
            (``422`` with the parser's message; the staged object is deleted).
    """
    target = _load_target(dataset_id)

    if target.source_type != SourceType.UPLOAD.value:
        raise AppValidationError(
            "This is a URL dataset; refresh it from its source URL, not a file."
        )

    # Re-validate the staged file. preview_upload deletes the object and raises
    # UploadInvalidError on an invalid file; the route maps that to 422.
    try:
        preview = upload_parser.preview_upload(upload_id)
    except upload_parser.UploadInvalidError as exc:
        raise AppValidationError(str(exc)) from exc

    # Stage mapping.json next to the upload so the Refresh Service has a concrete
    # mapping_key to copy into raw/v{n}/mapping.json. The document mirrors the
    # one create_from_upload writes (mapping + keep rule/count) so review-analysis
    # applies the same MAX_REVIEWS keep decision the analyst saw.
    mapping_key = keys.upload_mapping(upload_id)
    mapping_doc: dict[str, Any] = {
        "mapping": mapping,
        "keep_rule": preview.keep_rule,
        "will_keep": preview.will_keep,
        "usable_rows": preview.usable_rows,
    }
    s3.put_bytes(
        mapping_key,
        json.dumps(mapping_doc).encode("utf-8"),
        content_type="application/json",
    )

    capture = UploadCapture(
        file_key=keys.upload_file(upload_id),
        mapping_key=mapping_key,
    )
    return refresh_service.refresh(
        target.id,
        trigger="upload_replace",
        capture=capture,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_target(dataset_id: str) -> _RefreshTarget:
    """Load the dataset fields needed to refresh it, or ``404``."""
    with session_scope() as session:
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:
            raise NotFoundError("Dataset not found")
        status_value = (
            dataset.status.value
            if isinstance(dataset.status, DatasetStatus)
            else str(dataset.status)
        )
        source_value = (
            dataset.source_type.value
            if isinstance(dataset.source_type, SourceType)
            else str(dataset.source_type)
        )
        detail = dataset.status_detail if isinstance(dataset.status_detail, dict) else {}
        raw_check = detail.get("refresh_check_id")
        return _RefreshTarget(
            id=dataset.id,
            source_type=source_value,
            original_url=dataset.original_url,
            status=status_value,
            refresh_check_id=str(raw_check) if raw_check else None,
        )


def _guard_not_refreshing(target: _RefreshTarget) -> None:
    """Raise ``409 ALREADY_REFRESHING`` when a refresh is already in flight.

    A refresh is in flight when the dataset is ``requested``/``processing``
    (processing a new version), or when a refresh Check is still outstanding —
    recorded on ``status_detail.refresh_check_id`` and whose Check item is still
    running or waiting for confirmation (Requirement 4.4). A recorded id whose
    Check has since expired or resolved does **not** block a new refresh, so a
    dataset is never wedged by a stale marker.
    """
    if target.status in _IN_FLIGHT:
        raise ConflictError(
            "This dataset is already being processed.",
            code=ALREADY_REFRESHING_CODE,
        )
    if target.refresh_check_id and _refresh_check_outstanding(target.refresh_check_id):
        raise ConflictError(
            "A refresh is already running for this dataset.",
            code=ALREADY_REFRESHING_CODE,
        )


def _refresh_check_outstanding(check_id: str) -> bool:
    """True when the recorded refresh Check is still running or awaiting confirm.

    Reads the Check Session back from the store; an expired/absent session or an
    item that has already resolved (``done``/``applied``/``error``) is **not**
    outstanding, so a stale ``refresh_check_id`` never permanently blocks a new
    refresh.
    """
    session = check_session.get_session(check_id)
    if session is None:
        return False
    item = session.items.get(REFRESH_ITEM_ID)
    if item is None:
        return False
    return item.state in _OUTSTANDING_ITEM_STATES


def _set_refresh_check_id(dataset_id: str, check_id: str) -> None:
    """Record the running refresh Check id on ``status_detail.refresh_check_id``.

    Merged into the existing ``status_detail`` JSONB with ``||`` in a single
    ``UPDATE`` so the ``refresh_check_id`` key is added without disturbing the
    event log or other keys. The ``UPDATE`` takes the row's write lock for the
    duration of its transaction, so a concurrent ``status_detail`` writer (a
    status event append, another refresh start) serialises on it rather than
    clobbering it — the same effect as an explicit ``SELECT … FOR UPDATE``. The
    key is cleared by the Refresh Service when the Check resolves into a new
    version.
    """
    with session_scope() as session:
        stmt = text(
            """
            UPDATE datasets
            SET status_detail = COALESCE(status_detail, '{}'::jsonb) || :patch
            WHERE id = :id
            """
        ).bindparams(bindparam("patch", type_=JSONB))
        result = cast(
            "CursorResult[Any]",
            session.execute(stmt, {"patch": {"refresh_check_id": check_id}, "id": dataset_id}),
        )
        if result.rowcount == 0:  # pragma: no cover - caller already loaded the row
            raise NotFoundError("Dataset not found")


def clear_refresh_check_id(dataset_id: str) -> None:
    """Drop ``status_detail.refresh_check_id`` for a dataset (idempotent).

    Exposed so a resolution path that does **not** create a new data version —
    a ``wont_work`` refresh re-check that keeps the existing data (Requirement
    4.2) — can clear the "Checking page…" marker. (A successful refresh clears
    the key inside the Refresh Service's guarded write.) Removing an absent key
    is a no-op, so repeated calls are safe.
    """
    with session_scope() as session:
        session.execute(
            text(
                "UPDATE datasets SET status_detail = status_detail - 'refresh_check_id' "
                "WHERE id = :id"
            ).bindparams(id=dataset_id)
        )
