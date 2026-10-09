"""Library queries: list and read tracked datasets (dataset-library task 1).

This module owns the read side of the Dataset Library (design "API endpoints"
and "display_state" tables). It turns ``datasets`` / ``dataset_versions`` rows
into the shape the Library list and the detail page consume, deriving the fields
that the UI must not compute for itself so the list and the detail page always
agree:

- ``main_url`` — the original URL for a URL dataset, or the upload file name for
  an upload dataset (Requirement 2.1).
- ``last_message`` — the message of the most recent ``status_detail`` event, so
  a row can show the latest progress line ("Fetching page 3 of 10") or the last
  failure reason without the client parsing the event log.
- ``last_refreshed_at`` — ``dataset_versions.completed_at`` of the dataset's
  ``active_version`` (design: "``last_refreshed_at`` is
  ``dataset_versions.completed_at`` for ``active_version``").
- ``display_state`` — derived from ``(status, active_version)`` exactly per the
  design's table, so "Ready · refreshing" and "Ready · last refresh failed" are
  distinguished from a plain "Failed" (Requirement 2.5, Correctness Property 1).
- ``refresh_check_id`` — the id of a refresh Check that is running or waiting for
  confirmation, so the row can show "Checking page…" or "Needs confirmation".

The three operations are:

- :func:`list_datasets` — the ``GET /datasets`` query: the non-archived rows (or
  the archived ones when ``archived=True``), filtered by a case-insensitive
  text match on name or main URL, ordered by the requested sort
  (Requirements 2.1, 2.2, 2.3; Correctness Property 3).
- :func:`get_dataset` — the ``GET /datasets/{id}`` full record, including the
  ``active_version`` and the list of ``dataset_versions`` rows (Requirement 3.1).
- :func:`rename_dataset` — the ``PATCH /datasets/{id}`` rename.

Engineering rules honoured:

- **Stateless.** Pure reads/writes against Aurora; no process state.
- **DB only through ``core.db``** — every query runs inside ``session_scope``.
- Status is never changed here (rename touches only ``name``); every *status*
  change still flows through :func:`app.db.status.transition`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.db import session_scope
from app.core.errors import AppValidationError, NotFoundError
from app.db.models import Dataset, DatasetStatus, DatasetVersion, SourceType
from app.storage import keys, s3

#: The derived list-row ``display_state`` vocabulary (design "display_state"
#: table). It is a total function of ``(status, active_version)``.
DisplayState = Literal[
    "processing",
    "ready_refreshing",
    "ready",
    "ready_refresh_failed",
    "failed",
]

#: The sort orders the list accepts (Requirement 2.2). ``activity`` (last
#: activity, newest first) is the default; the analyst may also sort by name,
#: status, or last refreshed date.
SortKey = Literal["activity", "name", "status", "refreshed"]

#: Default sort when the request names none: last activity, newest first.
DEFAULT_SORT: SortKey = "activity"

#: The valid sort keys, exported so the API layer can validate the query param
#: without re-stating the literal set.
VALID_SORTS: frozenset[str] = frozenset(("activity", "name", "status", "refreshed"))


def derive_display_state(status: DatasetStatus | str, active_version: int | None) -> DisplayState:
    """Return the ``display_state`` for a ``(status, active_version)`` pair.

    This is the single source of truth for the design's ``display_state`` table,
    so the list and the detail page agree (Requirement 2.5). It is **total**:
    every status/active-version combination maps to exactly one state
    (Correctness Property 1).

    =========================  ===============  ======================
    status                     active_version   display_state
    =========================  ===============  ======================
    ``requested``/``processing``  null           ``processing``
    ``requested``/``processing``  set            ``ready_refreshing``
    ``updated``                   set            ``ready``
    ``failed``                    set            ``ready_refresh_failed``
    ``failed``                    null           ``failed``
    =========================  ===============  ======================

    ``updated`` with a null ``active_version`` cannot arise in practice (a
    dataset reaches ``updated`` only once a version finished, which sets
    ``active_version``); it is mapped to ``processing`` as the safe "no usable
    data yet" state so the function stays total for any input.
    """
    value = status.value if isinstance(status, DatasetStatus) else str(status)
    has_active = active_version is not None

    if value in (DatasetStatus.REQUESTED.value, DatasetStatus.PROCESSING.value):
        return "ready_refreshing" if has_active else "processing"
    if value == DatasetStatus.UPDATED.value:
        return "ready" if has_active else "processing"
    if value == DatasetStatus.FAILED.value:
        return "ready_refresh_failed" if has_active else "failed"
    # Unknown status (should never happen; the enum is DB-constrained): treat as
    # "no usable data" so the function remains total.
    return "ready" if has_active else "processing"


# ---------------------------------------------------------------------------
# Row / detail value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatasetVersionView:
    """One ``dataset_versions`` row as returned by the detail endpoint."""

    version: int
    trigger: str
    requested_at: str | None
    completed_at: str | None
    review_count: int | None
    extraction_method: str | None
    outcome: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "trigger": self.trigger,
            "requested_at": self.requested_at,
            "completed_at": self.completed_at,
            "review_count": self.review_count,
            "extraction_method": self.extraction_method,
            "outcome": self.outcome,
        }


@dataclass(frozen=True)
class DatasetRow:
    """One row of the ``GET /datasets`` list (design "API endpoints" table)."""

    id: str
    name: str
    main_url: str | None
    platform: str | None
    status: str
    display_state: DisplayState
    last_message: str | None
    review_count: int | None
    data_version: int
    active_version: int | None
    requested_at: str | None
    last_refreshed_at: str | None
    archived_at: str | None
    refresh_check_id: str | None
    #: "url" | "upload" — lets the UI pick a source-aware thumbnail fallback
    #: (CSV icon for uploads) without inferring upload-ness from a null URL.
    source_type: str
    #: Short-lived presigned GET for the active version's page snapshot, or
    #: ``None`` when there is no snapshot to show (upload datasets never have
    #: one; a URL dataset has none until its first successful version). The URL
    #: is signed without a HEAD check, so the browser falls back to the
    #: placeholder if the object is absent (see library design "Thumbnails").
    thumbnail_url: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "main_url": self.main_url,
            "platform": self.platform,
            "status": self.status,
            "display_state": self.display_state,
            "last_message": self.last_message,
            "review_count": self.review_count,
            "data_version": self.data_version,
            "active_version": self.active_version,
            "requested_at": self.requested_at,
            "last_refreshed_at": self.last_refreshed_at,
            "archived_at": self.archived_at,
            "refresh_check_id": self.refresh_check_id,
            "source_type": self.source_type,
            "thumbnail_url": self.thumbnail_url,
        }


@dataclass(frozen=True)
class DatasetDetail:
    """The full ``GET /datasets/{id}`` record, including the versions list.

    Beyond the list-row fields and the per-dataset detail fields, this carries
    the two blocks the detail page (``ingestion-summary``) consumes but the list
    does not:

    - ``status_detail`` — the whole append-only ``status_detail`` JSONB document
      (the ``events`` log, plus the ``redirects`` and ``viability`` blocks and
      ``refresh_check_id``), returned as stored. The ProcessingTimeline reads
      ``events``, the DatasetHeader "Resolved to" hops read ``redirects``, and
      the PredictionPanel reads ``viability`` (ingestion-summary Requirements 1,
      6, 7). The frontend ``StatusDetail`` type tolerates extra keys, so the dict
      is passed through unreshaped.
    - ``description`` — the analyst-supplied upload source description. Uploads
      have no ``description`` column; ingestion stores it at the top level of
      ``status_detail`` (``app.ingestion.service._insert_upload_dataset``), so it
      is sourced from there and is ``None`` for URL datasets (and for uploads
      added without one).

    The identified-entity profile is **not** surfaced on ``metrics``:
    review-analysis writes the entity (name/category/description/confidence) into
    ``reviews/v{n}.json`` under ``entity``, never into the ``metrics`` dict
    (``app.handlers.metrics``), so EntityCard reads it from the reviews file.
    ``metrics`` is returned unchanged.
    """

    row: DatasetRow
    source_type: str
    original_url: str | None
    final_url: str | None
    page_title: str | None
    metrics: dict[str, Any] | None
    status_detail: dict[str, Any]
    description: str | None
    versions: list[DatasetVersionView]

    def to_dict(self) -> dict[str, Any]:
        data = self.row.to_dict()
        data.update(
            {
                "source_type": self.source_type,
                "original_url": self.original_url,
                "final_url": self.final_url,
                "page_title": self.page_title,
                "metrics": self.metrics,
                "status_detail": self.status_detail,
                "description": self.description,
                "versions": [v.to_dict() for v in self.versions],
            }
        )
        return data


# ---------------------------------------------------------------------------
# Field derivation helpers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    """Return an ISO-8601 string for a timestamp column, or ``None``."""
    return value.isoformat() if value is not None else None


def _main_url(dataset: Dataset) -> str | None:
    """The row's "main URL": the original URL, or the upload file name.

    For a URL dataset this is the original URL the analyst submitted. For an
    upload dataset there is no URL, so the design shows the upload file name;
    it is stored as the dataset ``name`` at upload time, so an upload row's
    ``main_url`` is its name (Requirement 2.1).
    """
    if dataset.source_type == SourceType.UPLOAD:
        return dataset.name
    return dataset.original_url


def _last_message(dataset: Dataset) -> str | None:
    """The message of the most recent ``status_detail`` event, if any.

    The event log is append-only and in time order (see :mod:`app.db.status`),
    so the last entry is the most recent progress or status message. A dataset
    always has at least a ``requested`` event, but this tolerates an empty log.
    """
    detail = dataset.status_detail or {}
    events = detail.get("events") if isinstance(detail, dict) else None
    if not events:
        return None
    last = events[-1]
    if isinstance(last, dict):
        message = last.get("message")
        return str(message) if message is not None else None
    return None


def _refresh_check_id(dataset: Dataset) -> str | None:
    """The id of an in-flight / awaiting-confirmation refresh Check, if any.

    While a refresh Check is running or waiting for the analyst to confirm a
    ``limited`` verdict, the row shows "Checking page…" or "Needs confirmation"
    (design "display_state" note). The refresh endpoint (dataset-library task 3)
    records that check id on ``status_detail.refresh_check_id`` when it starts
    the refresh Check and clears it when the Check resolves, so the list and
    detail responses surface it without querying the DynamoDB Check store (which
    has no index by dataset id). This reads that field; it is ``None`` whenever
    no refresh Check is outstanding.
    """
    detail = dataset.status_detail or {}
    if not isinstance(detail, dict):
        return None
    value = detail.get("refresh_check_id")
    return str(value) if value else None


def _upload_description(dataset: Dataset) -> str | None:
    """The analyst-supplied upload source description, or ``None``.

    There is no ``description`` column on ``datasets``; ingestion stores the
    optional upload source description at the top level of ``status_detail``
    (``app.ingestion.service._insert_upload_dataset`` writes
    ``status_detail["description"]``). This reads it back from there, so the
    detail page can show it without a schema change. It is ``None`` for URL
    datasets (which never set it) and for uploads submitted without one.
    """
    detail = dataset.status_detail or {}
    if not isinstance(detail, dict):
        return None
    value = detail.get("description")
    return str(value) if isinstance(value, str) and value else None


def _active_completed_at(dataset: Dataset, versions: list[DatasetVersion]) -> str | None:
    """``dataset_versions.completed_at`` for the dataset's ``active_version``.

    This is the row's ``last_refreshed_at`` (design: "``last_refreshed_at`` is
    ``dataset_versions.completed_at`` for ``active_version``"). ``None`` when the
    dataset has no active version yet, or the active version's row has not been
    marked complete.
    """
    if dataset.active_version is None:
        return None
    for version in versions:
        if version.version == dataset.active_version:
            return _iso(version.completed_at)
    return None


#: Lifetime of a list-row thumbnail link. Short-lived (design uses 5 min).
_THUMBNAIL_URL_TTL_SECONDS = 300


def _source_type_value(dataset: Dataset) -> str:
    """The dataset's source type as a plain string ("url" | "upload")."""
    return (
        dataset.source_type.value
        if isinstance(dataset.source_type, SourceType)
        else str(dataset.source_type)
    )


def _thumbnail_url(dataset: Dataset) -> str | None:
    """Presigned GET for the active version's page snapshot, or ``None``.

    Returns ``None`` for upload datasets (no page screenshot exists —
    Requirement 7.5; the UI shows a CSV icon instead) and for any dataset
    without an ``active_version`` yet (no completed version to show). The key is
    built from :func:`app.storage.keys.dataset_snapshot`; the URL is signed
    WITHOUT a per-row S3 HEAD (keeping a long list cheap), so if the object is
    absent the browser simply falls back to the placeholder.
    """
    if dataset.source_type == SourceType.UPLOAD:
        return None
    if dataset.active_version is None:
        return None
    key = keys.dataset_snapshot(dataset.id, dataset.active_version)
    return s3.presign_get(key, expires_in=_THUMBNAIL_URL_TTL_SECONDS)


def _to_row(dataset: Dataset, versions: list[DatasetVersion]) -> DatasetRow:
    """Build a :class:`DatasetRow` from a dataset and its version rows."""
    status_value = (
        dataset.status.value if isinstance(dataset.status, DatasetStatus) else str(dataset.status)
    )
    review_count = None
    metrics = dataset.metrics
    if isinstance(metrics, dict):
        raw_count = metrics.get("review_count")
        if isinstance(raw_count, int):
            review_count = raw_count

    return DatasetRow(
        id=dataset.id,
        name=dataset.name,
        main_url=_main_url(dataset),
        platform=dataset.platform,
        status=status_value,
        display_state=derive_display_state(dataset.status, dataset.active_version),
        last_message=_last_message(dataset),
        review_count=review_count,
        data_version=dataset.data_version,
        active_version=dataset.active_version,
        requested_at=_iso(dataset.requested_at),
        last_refreshed_at=_active_completed_at(dataset, versions),
        archived_at=_iso(dataset.archived_at),
        refresh_check_id=_refresh_check_id(dataset),
        source_type=_source_type_value(dataset),
        thumbnail_url=_thumbnail_url(dataset),
    )


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


def _order_by(sort: SortKey) -> list[Any]:
    """Return the ORDER BY expression(s) for a sort key.

    - ``activity``: last activity, newest first — ``updated_at DESC``
      (``updated_at`` is bumped on every status change, so it tracks activity).
    - ``name``: case-insensitive ascending by name.
    - ``status``: by status, then newest activity within a status. ``status`` is
      a native PostgreSQL enum, so ``ORDER BY status`` sorts by the enum's
      DECLARATION order (``requested``, ``processing``, ``updated``, ``failed``),
      not alphabetically. That declaration order is the dataset lifecycle, which
      is the grouping Requirement 2.2's "sort by status" intends — in-progress
      datasets first, terminal states last — so this is intentional (e.g.
      ``updated`` sorts before ``failed``), not an alphabetical sort.
    - ``refreshed``: by last update time (the row's last-refreshed proxy),
      newest first. ``last_refreshed_at`` lives on ``dataset_versions`` and is
      derived per row; ``updated_at`` moves with every refresh transition, so it
      orders rows by refresh recency without a join.

    Every order has ``id`` appended as a deterministic tie-breaker so a stable
    order is returned for rows that compare equal (Correctness Property 3's
    "requested order").
    """
    if sort == "name":
        return [Dataset.name.collate("C").asc(), Dataset.id.asc()]
    if sort == "status":
        return [Dataset.status.asc(), Dataset.updated_at.desc(), Dataset.id.asc()]
    if sort == "refreshed":
        return [Dataset.updated_at.desc(), Dataset.id.asc()]
    # activity (default)
    return [Dataset.updated_at.desc(), Dataset.id.asc()]


# ---------------------------------------------------------------------------
# Public queries
# ---------------------------------------------------------------------------


def list_datasets(
    *,
    archived: bool = False,
    sort: str = DEFAULT_SORT,
    q: str | None = None,
) -> list[DatasetRow]:
    """Return the tracked datasets for the Library list.

    Args:
        archived: When ``False`` (default) return only non-archived datasets
            (Requirement 2.1); when ``True`` return only archived datasets (the
            "Show archived" toggle, Requirement 5.2).
        sort: One of ``activity`` (default), ``name``, ``status``, ``refreshed``
            (Requirement 2.2). An unknown value falls back to ``activity``.
        q: Optional case-insensitive text filter; a row matches when the text is
            a substring of its name or its main URL (the original URL for a URL
            dataset, or — via the stored name — an upload) (Requirement 2.3,
            Correctness Property 3). Blank/whitespace text applies no filter.

    Returns:
        The matching rows in the requested order, each with its derived
        ``display_state``, ``main_url``, ``last_message``, ``last_refreshed_at``,
        and ``refresh_check_id``.
    """
    sort_key: SortKey = sort if sort in VALID_SORTS else DEFAULT_SORT  # type: ignore[assignment]

    stmt = select(Dataset)
    if archived:
        stmt = stmt.where(Dataset.archived_at.is_not(None))
    else:
        stmt = stmt.where(Dataset.archived_at.is_(None))

    text = (q or "").strip()
    if text:
        pattern = f"%{_escape_like(text)}%"
        # Case-insensitive substring match on name OR the main URL. For URL
        # datasets the main URL is ``original_url``; for uploads it is the name,
        # which the name clause already covers.
        stmt = stmt.where(
            or_(
                Dataset.name.ilike(pattern, escape="\\"),
                Dataset.original_url.ilike(pattern, escape="\\"),
            )
        )

    stmt = stmt.order_by(*_order_by(sort_key))

    with session_scope() as session:
        datasets = list(session.scalars(stmt).all())
        if not datasets:
            return []
        versions_by_dataset = _load_versions(session, [d.id for d in datasets])
        return [_to_row(d, versions_by_dataset.get(d.id, [])) for d in datasets]


def get_dataset(dataset_id: str) -> DatasetDetail:
    """Return the full record for one dataset (``GET /datasets/{id}``).

    Includes the derived list fields, the ``active_version``, and the dataset's
    ``dataset_versions`` rows ordered by version (Requirement 3.1), plus the
    detail-only ``status_detail`` document (``events`` log, ``redirects``, and
    ``viability`` blocks) and the upload ``description`` the detail page consumes
    (Requirements 3.1, 6.4). The list response (``GET /datasets``) is unchanged.
    The detail page URL is stable and loads directly for anyone who opens it.

    Raises:
        NotFoundError: When no dataset has that id (``404``).
    """
    with session_scope() as session:
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:
            raise NotFoundError("Dataset not found")
        versions = _load_versions(session, [dataset_id]).get(dataset_id, [])
        row = _to_row(dataset, versions)
        return DatasetDetail(
            row=row,
            source_type=(
                dataset.source_type.value
                if isinstance(dataset.source_type, SourceType)
                else str(dataset.source_type)
            ),
            original_url=dataset.original_url,
            final_url=dataset.final_url,
            page_title=dataset.page_title,
            metrics=dataset.metrics,
            status_detail=dataset.status_detail or {"events": []},
            description=_upload_description(dataset),
            versions=[_to_version_view(v) for v in versions],
        )


def rename_dataset(dataset_id: str, name: str) -> DatasetDetail:
    """Rename a dataset (``PATCH /datasets/{id}``) and return the updated record.

    Only the ``name`` column is changed; no status transition and no other field
    is touched. A blank/whitespace name is rejected as a ``422`` so a row is
    never left nameless.

    Raises:
        NotFoundError: When no dataset has that id (``404``).
        AppValidationError: When ``name`` is empty or whitespace (``422``).
    """
    cleaned = name.strip()
    if not cleaned:
        raise AppValidationError("A dataset name is required")

    with session_scope() as session:
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:
            raise NotFoundError("Dataset not found")
        dataset.name = cleaned
    # Re-read so the returned detail reflects the committed row.
    return get_dataset(dataset_id)


def archive_dataset(dataset_id: str) -> DatasetDetail:
    """Archive a dataset (``POST /datasets/{id}/archive``) and return its record.

    Sets ``archived_at`` so the dataset drops out of the default list and is
    only shown under the "Show archived" toggle (Requirements 5.1, 5.2). This
    touches **only** the ``archived_at`` column: no status transition, no other
    field, and — crucially — no ``datasets`` or ``dataset_versions`` row and no
    S3 object is ever deleted (Requirements 5.3, 5.4; Correctness Property 2).
    Archiving is not a lifecycle status change, so it does not flow through
    :func:`app.db.status.transition`; the status column is left untouched.

    Idempotent: archiving an already-archived dataset is a safe no-op — the
    existing ``archived_at`` is preserved rather than overwritten, so repeated
    or concurrent calls converge on the same state (idempotent-handler rule).

    Raises:
        NotFoundError: When no dataset has that id (``404``).
    """
    with session_scope() as session:
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:
            raise NotFoundError("Dataset not found")
        if dataset.archived_at is None:
            dataset.archived_at = datetime.now(UTC)
        # Already archived: leave the original timestamp (no-op).
    # Re-read so the returned detail reflects the committed row.
    return get_dataset(dataset_id)


def restore_dataset(dataset_id: str) -> DatasetDetail:
    """Restore a dataset (``POST /datasets/{id}/restore``) and return its record.

    Clears ``archived_at`` so the dataset returns to the default list
    (Requirement 5.3). Like :func:`archive_dataset`, this changes **only**
    ``archived_at``: no status transition, no other field, and no row or S3
    object is deleted (Requirements 5.3, 5.4; Correctness Property 2).

    Idempotent: restoring an active (non-archived) dataset is a safe no-op, so
    repeated or concurrent calls converge on the same state.

    Raises:
        NotFoundError: When no dataset has that id (``404``).
    """
    with session_scope() as session:
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:
            raise NotFoundError("Dataset not found")
        dataset.archived_at = None
    # Re-read so the returned detail reflects the committed row.
    return get_dataset(dataset_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_versions(session: Session, dataset_ids: list[str]) -> dict[str, list[DatasetVersion]]:
    """Load ``dataset_versions`` rows for the given datasets, grouped by id.

    One query fetches every version row for the listed datasets (so the list
    endpoint does not issue a query per row), ordered by ``(dataset_id,
    version)`` so each dataset's versions are in ascending version order.
    """
    if not dataset_ids:
        return {}
    stmt = (
        select(DatasetVersion)
        .where(DatasetVersion.dataset_id.in_(dataset_ids))
        .order_by(DatasetVersion.dataset_id, DatasetVersion.version)
    )
    grouped: dict[str, list[DatasetVersion]] = {}
    for version in session.scalars(stmt).all():
        grouped.setdefault(version.dataset_id, []).append(version)
    return grouped


def _to_version_view(version: DatasetVersion) -> DatasetVersionView:
    """Serialize one :class:`DatasetVersion` for the detail response."""
    return DatasetVersionView(
        version=version.version,
        trigger=version.trigger,
        requested_at=_iso(version.requested_at),
        completed_at=_iso(version.completed_at),
        review_count=version.review_count,
        extraction_method=version.extraction_method,
        outcome=version.outcome,
    )


def _escape_like(text: str) -> str:
    r"""Escape LIKE/ILIKE wildcards so a search term matches literally.

    A user's search text may contain ``%`` or ``_`` (both LIKE wildcards) or the
    escape character ``\\``; escaping them means the filter matches those
    characters literally rather than treating them as patterns (Correctness
    Property 3: the match is an exact case-insensitive substring).
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
