"""Library API router: the dataset list, detail, and rename endpoints.

This router supplies the read/rename endpoints from the dataset-library design's
"API endpoints" table, mounted under the ``/api`` prefix by :mod:`app.api`:

- ``GET /api/datasets?archived=&sort=&q=`` — the Tracked Datasets list. Returns
  one object per dataset with the derived ``main_url``, ``last_message``,
  ``last_refreshed_at``, ``display_state``, and ``refresh_check_id``
  (Requirements 2.1, 2.2, 2.3, 2.5).
- ``GET /api/datasets/{id}`` — the full record used by the detail page and
  ``ingestion-summary``, including ``active_version`` and the ``dataset_versions``
  list (Requirement 3.1). ``404`` when the id is unknown.
- ``PATCH /api/datasets/{id}`` — rename a dataset (``{name}``). ``404`` when the
  id is unknown, ``422`` when the name is empty.
- ``POST /api/datasets/{id}/archive`` — set ``archived_at`` so the dataset drops
  out of the default list (Requirements 5.1, 5.4). ``404`` when unknown.
- ``POST /api/datasets/{id}/restore`` — clear ``archived_at`` so the dataset
  returns to the default list (Requirement 5.3). ``404`` when unknown.

All domain logic lives in :mod:`app.datasets.library`; this router is a thin
controller that validates the query parameters, delegates, and serializes the
result. It raises the shared :class:`~app.core.errors.AppError` subclasses so
the ``{"error": {"code", "message"}}`` envelope is produced by the app's error
handlers. Nothing here imports a Lambda event shape — it is plain FastAPI that
runs unchanged in both compute modes.

- ``POST /api/datasets/{id}/refresh`` — start a refresh of a URL dataset from
  its source URL. Returns ``202 {check_id}``; ``409 ALREADY_REFRESHING`` while a
  refresh is in flight (Requirements 4.1, 4.2, 4.4).
- ``POST /api/datasets/{id}/refresh/confirm`` — confirm a ``limited`` refresh
  verdict and proceed (Requirement 4.3).
- ``POST /api/datasets/{id}/refresh/upload`` — refresh an upload dataset by
  replacing its data from a staged upload (Requirement 4.5).

The ingestion-owned ``POST /datasets/upload`` lives on a separate router in
:mod:`app.ingestion.api` (dataset-ingestion); this router carries only the
Library-owned ``/datasets`` endpoints. The refresh endpoints delegate to
:mod:`app.datasets.refresh_api` (dataset-library task 3).
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.datasets import library, refresh_api

router = APIRouter(prefix="/datasets", tags=["library"])


class RenameRequest(BaseModel):
    """Body of ``PATCH /datasets/{id}``: the new dataset name.

    ``name`` carries a ``min_length=1`` bound so a missing or empty name is a
    plain ``422`` from request validation; a whitespace-only name is rejected by
    :func:`app.datasets.library.rename_dataset` with the same code.
    """

    name: str = Field(..., min_length=1)


class RefreshConfirmRequest(BaseModel):
    """Body of ``POST /datasets/{id}/refresh/confirm``: the Check to confirm.

    ``check_id`` names the refresh Check whose ``limited`` verdict the analyst is
    confirming (Requirement 4.3); its ``min_length=1`` bound makes a missing id a
    plain ``422``.
    """

    check_id: str = Field(..., min_length=1)


class RefreshUploadRequest(BaseModel):
    """Body of ``POST /datasets/{id}/refresh/upload`` (Requirement 4.5).

    ``upload_id`` names the staged replacement file (from the pre-signed upload
    flow); ``mapping`` is the analyst-confirmed canonical-field → header-name
    column mapping reviewed in the preview. ``upload_id``'s ``min_length=1``
    bound makes a missing id a plain ``422``.
    """

    upload_id: str = Field(..., min_length=1)
    mapping: dict[str, str] = Field(default_factory=dict)


@router.get("")
def list_datasets(
    archived: bool = Query(default=False),
    sort: str = Query(default=library.DEFAULT_SORT),
    q: str | None = Query(default=None),
) -> dict[str, object]:
    """List tracked datasets (``GET /datasets``).

    - ``archived`` (default ``false``): return the non-archived datasets, or the
      archived ones when ``true`` (Requirements 2.1, 5.2).
    - ``sort`` (default ``activity``): one of ``activity`` / ``name`` /
      ``status`` / ``refreshed`` (Requirement 2.2). An unknown value falls back
      to ``activity`` in the query layer.
    - ``q``: optional case-insensitive text filter on name or main URL
      (Requirement 2.3).

    Returns ``{"datasets": [...]}`` with each row carrying its derived fields.
    """
    rows = library.list_datasets(archived=archived, sort=sort, q=q)
    return {"datasets": [row.to_dict() for row in rows]}


@router.get("/{dataset_id}")
def get_dataset(dataset_id: str) -> dict[str, object]:
    """Return one dataset's full record (``GET /datasets/{id}``).

    Includes the derived list fields, ``active_version``, and the
    ``dataset_versions`` list (Requirement 3.1). Raises ``404 NOT_FOUND`` when
    the id is unknown.
    """
    return library.get_dataset(dataset_id).to_dict()


@router.patch("/{dataset_id}")
def rename_dataset(dataset_id: str, body: RenameRequest) -> dict[str, object]:
    """Rename a dataset (``PATCH /datasets/{id}``).

    Changes only the name and returns the updated full record. Raises ``404``
    when the id is unknown and ``422`` when the name is empty/whitespace.
    """
    return library.rename_dataset(dataset_id, body.name).to_dict()


@router.post("/{dataset_id}/archive")
def archive_dataset(dataset_id: str) -> dict[str, object]:
    """Archive a dataset (``POST /datasets/{id}/archive``).

    Sets ``archived_at`` so the dataset drops out of the default list and is
    only shown under the "Show archived" toggle (Requirements 5.1, 5.2). No
    database row and no S3 object is ever deleted (Requirements 5.3, 5.4). The
    returned record carries ``archived_at`` so the detail page and chat can go
    read-only (that chat behaviour is built in ``guardrailed-chat``; this
    endpoint only exposes the field). Archiving an already-archived dataset is a
    tolerant no-op. Raises ``404`` when the id is unknown.
    """
    return library.archive_dataset(dataset_id).to_dict()


@router.post("/{dataset_id}/restore")
def restore_dataset(dataset_id: str) -> dict[str, object]:
    """Restore a dataset (``POST /datasets/{id}/restore``).

    Clears ``archived_at`` so the dataset returns to the default list
    (Requirement 5.3). Like archive, it changes only ``archived_at`` and deletes
    nothing. Restoring an active (non-archived) dataset is a tolerant no-op.
    Raises ``404`` when the id is unknown.
    """
    return library.restore_dataset(dataset_id).to_dict()


@router.post("/{dataset_id}/refresh", status_code=202)
def refresh_dataset(dataset_id: str) -> dict[str, str]:
    """Refresh a URL dataset from its source (``POST /datasets/{id}/refresh``).

    Starts a refresh-origin Check of the dataset's original URL, enqueues it,
    records the running Check id so the row shows "Checking page…", and returns
    ``202 {check_id}`` (Requirements 4.1, 4.2). The check handler then completes
    the refresh automatically, waits for confirmation, or records a failure.

    - ``404`` when the id is unknown.
    - ``422`` when the dataset is an upload dataset (use ``/refresh/upload``) or
      has no source URL.
    - ``409 ALREADY_REFRESHING`` while the dataset is ``requested``/
      ``processing`` or a refresh Check is already running (Requirement 4.4).
    """
    check_id = refresh_api.start_refresh(dataset_id)
    return {"check_id": check_id}


@router.post("/{dataset_id}/refresh/confirm")
def confirm_refresh(dataset_id: str, body: RefreshConfirmRequest) -> dict[str, str]:
    """Confirm a ``limited`` refresh verdict (``POST /datasets/{id}/refresh/confirm``).

    Proceeds with the refresh the analyst confirmed, re-using the Check's capture
    and ``trigger="manual_refresh"`` (Requirement 4.3). Returns the Refresh
    Service outcome. Raises ``404`` when the dataset or Check is unknown/expired
    or the Check is not awaiting confirmation.
    """
    outcome = refresh_api.confirm_refresh(dataset_id, body.check_id)
    return {"outcome": outcome}


@router.post("/{dataset_id}/refresh/upload")
def refresh_dataset_from_upload(dataset_id: str, body: RefreshUploadRequest) -> dict[str, str]:
    """Refresh an upload dataset from a replacement file.

    Uses the pre-signed upload flow and ``trigger="upload_replace"`` to process
    the replacement as a new data version (Requirement 4.5). Returns the Refresh
    Service outcome. Raises ``404`` when the id is unknown and ``422`` when the
    dataset is a URL dataset or the staged file is invalid.
    """
    outcome = refresh_api.refresh_from_upload(dataset_id, body.upload_id, body.mapping)
    return {"outcome": outcome}
