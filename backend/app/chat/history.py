"""Shared Q&A history timeline (guardrailed-chat Task 5.1).

The history API returns one **shared** timeline per dataset: every saved
Exchange from every conversation (Requirement 5.6), merged with the dataset's
*refresh markers*, in time order, oldest first (Requirement 5.2). The analyst
reads it to see which earlier answers may no longer apply after a refresh
(Requirement 9).

Two sources feed the timeline (design "Refresh markers"):

1. **Exchange objects** under ``datasets/{id}/chat/`` — the Q&A entries Task 4.3
   writes (design "Data Models"). Each Exchange carries ``is_stale`` here: its
   ``data_version`` is older than the dataset's ``active_version``
   (Requirement 9.4).
2. **``dataset_versions`` rows where ``version > 1``** — each becomes one refresh
   marker. The first version is the dataset's initial ingestion, not a refresh,
   so it never produces a marker. A refresh that fails at the URL check never
   creates a version row at all, so it never produces a marker either
   (Requirement 9.8) — nothing special is coded for that case; it falls out of
   "markers come only from real version rows".

The merge, ordering, stale-flagging, and backward paging are all pure functions
(:func:`merge_timeline`, :func:`build_markers`) so they are unit-testable
without S3 or a database. :func:`load_history` is the thin reader that pulls the
Exchanges from S3 (reusing the chat prefix and listing from
:mod:`app.storage.keys` / :mod:`app.storage.s3`, the same objects
:func:`app.chat.assembly.load_recent_exchanges` reads) and the version rows and
``active_version`` from the database through :mod:`app.core.db`, then delegates
to the pure merge.

Paging (Requirement 5.3) is **backward** by a single shared timestamp cursor:
``before`` is an ISO-8601 timestamp; the page is the newest :data:`PAGE_SIZE`
items whose timeline timestamp sorts strictly before it. Because markers and
Exchanges share one cursor, they page together — a page boundary can fall
between a marker and an Exchange, and the next ``before`` call picks up exactly
where this one stopped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.db import session_scope
from app.db.models import Dataset, DatasetVersion
from app.storage import keys, s3

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Tunables (design "Endpoints" / Requirement 5.3)
# ---------------------------------------------------------------------------

#: History page size. The design and Requirement 5.3 fix this at 20: the history
#: pane loads earlier Exchanges in pages of 20 as the analyst scrolls up.
PAGE_SIZE = 20

#: Item ``type`` discriminators the frontend keys off when rendering a timeline
#: row (an Exchange row vs. a full-width :class:`RefreshMarker`).
EXCHANGE_TYPE = "exchange"
MARKER_TYPE = "refresh_marker"

#: Marker states (design "Refresh markers"). ``completed`` once the new version
#: finished successfully, ``failed`` when the refresh failed, ``pending`` while
#: it is still ``requested``/``processing`` (no terminal outcome yet).
MARKER_COMPLETED = "completed"
MARKER_PENDING = "pending"
MARKER_FAILED = "failed"

#: ``dataset_versions.outcome`` written by review-analysis / the sweeper on a
#: terminal version (``completion.py`` sets ``updated``; ``sweep.py`` sets
#: ``failed``). A row with ``outcome IS NULL`` is still in flight → pending.
_OUTCOME_UPDATED = "updated"
_OUTCOME_FAILED = "failed"


# ---------------------------------------------------------------------------
# Version record (marker input) — a plain view of a dataset_versions row
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VersionRecord:
    """One ``dataset_versions`` row reduced to what a refresh marker needs.

    Carrying a plain view (not a live ORM row) keeps the merge pure and lets
    tests build markers without a database. The fields mirror the
    ``dataset_versions`` columns the design's marker shape uses.

    :ivar version: The data version number (markers are built only for
        ``version > 1``).
    :ivar trigger: What started the version — ``manual_refresh``,
        ``duplicate_submission``, or ``upload_replace`` (the initial version's
        ``add``/``upload`` trigger never reaches a marker because version 1 is
        skipped).
    :ivar requested_at: ISO-8601 time the version was requested; the marker's
        timeline position while it is still pending.
    :ivar completed_at: ISO-8601 completion time, or ``None`` while in flight;
        the marker's timeline position once terminal.
    :ivar review_count: Review count of this version (``None`` until completion).
    :ivar outcome: ``"updated"`` (completed), ``"failed"``, or ``None`` (pending).
    """

    version: int
    trigger: str
    requested_at: str | None
    completed_at: str | None
    review_count: int | None
    outcome: str | None

    @classmethod
    def from_row(cls, row: DatasetVersion) -> VersionRecord:
        """Build a :class:`VersionRecord` from a ``dataset_versions`` ORM row."""
        return cls(
            version=row.version,
            trigger=row.trigger,
            requested_at=_iso(row.requested_at),
            completed_at=_iso(row.completed_at),
            review_count=row.review_count,
            outcome=row.outcome,
        )


# ---------------------------------------------------------------------------
# Timeline item + page result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineItem:
    """One row in the merged timeline: an Exchange or a refresh marker.

    :ivar cursor: The timestamp this item is placed at in the merged timeline —
        an Exchange's ``asked_at``, or a marker's ``completed_at`` (terminal) or
        ``requested_at`` (pending). Paging compares ``before`` against this.
    :ivar payload: The serialized item the API returns. Exchanges carry
        ``type="exchange"`` and an ``is_stale`` flag; markers carry
        ``type="refresh_marker"`` and the design's marker fields.
    """

    cursor: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """A single page of the shared timeline, oldest first.

    :ivar items: The page's serialized timeline items (Exchanges + markers),
        oldest first (Requirement 5.2).
    :ivar next_before: The cursor to pass as ``before`` to fetch the previous
        (older) page, or ``None`` when the oldest item has been returned — i.e.
        this page is the start of the timeline. It is the timeline timestamp of
        the oldest item on this page.
    :ivar has_more: ``True`` when older items exist beyond this page.
    """

    items: list[dict[str, Any]]
    next_before: str | None
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the JSON response (``{items, next_before, has_more}``)."""
        return {
            "items": self.items,
            "next_before": self.next_before,
            "has_more": self.has_more,
        }


# ---------------------------------------------------------------------------
# Pure marker building
# ---------------------------------------------------------------------------


def _marker_state(outcome: str | None, completed_at: str | None) -> str:
    """Classify a version row into a marker state (design "Refresh markers").

    - ``completed`` — the version finished successfully (``outcome == 'updated'``
      and it has a ``completed_at``).
    - ``failed`` — the version's refresh failed (``outcome == 'failed'``).
    - ``pending`` — no terminal outcome yet (still ``requested``/``processing``).

    A row with ``outcome == 'updated'`` but somehow no ``completed_at`` is
    treated as pending: without a completion time there is no terminal point to
    place a completed marker at, and the next poll will carry the timestamp.
    """
    if outcome == _OUTCOME_FAILED:
        return MARKER_FAILED
    if outcome == _OUTCOME_UPDATED and completed_at is not None:
        return MARKER_COMPLETED
    return MARKER_PENDING


def _marker_cursor(record: VersionRecord, state: str) -> str | None:
    """The timeline timestamp a marker sits at.

    A terminal marker (completed/failed) sits at ``completed_at`` — the moment
    the new data version became ready (or the refresh failed). A pending marker
    sits at ``requested_at`` — the design places the pending marker "at the end
    of the history" while the refresh runs, and a refresh's ``requested_at`` is
    later than every Exchange answered before it. Returns ``None`` when the row
    carries no usable timestamp (it is then skipped rather than placed at an
    unknown point).
    """
    if state == MARKER_PENDING:
        return record.requested_at or record.completed_at
    return record.completed_at or record.requested_at


def build_markers(records: Sequence[VersionRecord]) -> list[TimelineItem]:
    """Build one refresh marker per version after the first (design Property 3).

    Only ``version > 1`` rows become markers (version 1 is the initial
    ingestion, not a refresh). Each marker carries its ``state``, ``trigger``,
    both timestamps, this version's ``review_count``, and
    ``previous_review_count`` — the ``review_count`` of the immediately prior
    version by number (design "Refresh markers": "212 reviews (was 180)"), or
    ``None`` when the prior version's count is unknown or absent.

    The input need not be sorted; the previous-count lookup is built from a
    version→count map so it is order-independent, and the returned markers are
    not sorted here (the merge orders everything together).
    """
    count_by_version = {record.version: record.review_count for record in records}
    markers: list[TimelineItem] = []
    for record in records:
        if record.version <= 1:
            continue
        state = _marker_state(record.outcome, record.completed_at)
        cursor = _marker_cursor(record, state)
        if cursor is None:
            continue
        payload: dict[str, Any] = {
            "type": MARKER_TYPE,
            "version": record.version,
            "state": state,
            "trigger": record.trigger,
            "requested_at": record.requested_at,
            "completed_at": record.completed_at,
            "review_count": record.review_count,
            "previous_review_count": count_by_version.get(record.version - 1),
        }
        markers.append(TimelineItem(cursor=cursor, payload=payload))
    return markers


# ---------------------------------------------------------------------------
# Pure Exchange building
# ---------------------------------------------------------------------------


def _exchange_item(exchange: dict[str, Any], active_version: int | None) -> TimelineItem:
    """Wrap a saved Exchange as a timeline item with its ``is_stale`` flag.

    ``is_stale`` is true when the Exchange's ``data_version`` is older than the
    dataset's ``active_version`` (Requirement 9.4) — the reviews have changed
    since it was answered. When the dataset has no ``active_version`` (its first
    processing hasn't finished) nothing is stale. The full saved Exchange is
    returned unchanged except for the added ``type`` and ``is_stale`` fields, so
    the frontend keeps the saved citation snippets (Requirement 2.2).
    """
    data_version = int(exchange.get("data_version", 0))
    is_stale = active_version is not None and data_version < active_version
    payload = dict(exchange)
    payload["type"] = EXCHANGE_TYPE
    payload["is_stale"] = is_stale
    return TimelineItem(cursor=str(exchange.get("asked_at", "")), payload=payload)


# ---------------------------------------------------------------------------
# Pure merge + paging
# ---------------------------------------------------------------------------


def merge_timeline(
    exchanges: Sequence[dict[str, Any]],
    records: Sequence[VersionRecord],
    *,
    active_version: int | None,
    before: str | None = None,
    limit: int = PAGE_SIZE,
) -> HistoryPage:
    """Merge Exchanges and refresh markers into one backward-paged page.

    Builds marker items (:func:`build_markers`) and Exchange items
    (:func:`_exchange_item`, flagging stale ones), places them on a single
    shared timestamp cursor, and returns the newest *limit* items whose cursor
    sorts strictly before *before* — presented oldest first (Requirement 5.2).

    Backward paging (Requirement 5.3): ``before`` is the cursor returned as
    ``next_before`` from the previous (newer) page; passing it fetches the
    immediately older page. ``before=None`` returns the newest page (the end of
    the timeline). Because markers and Exchanges share the one cursor they page
    together, so a page boundary can fall between a marker and an Exchange and
    the next call resumes exactly there (design "Endpoints": "markers and
    Exchanges page together on a merged timestamp cursor").

    Ties on the cursor (an Exchange and a marker, or two Exchanges, at the same
    timestamp) are ordered deterministically by ``(cursor, type, version/asked)``
    via :func:`_sort_key` so paging is stable and never drops or repeats an item.

    :param exchanges: Saved Exchange documents (any order).
    :param records: ``dataset_versions`` views (any order; only ``version > 1``
        become markers).
    :param active_version: The dataset's current active version, for stale flags.
    :param before: Exclusive upper-bound cursor; ``None`` for the newest page.
    :param limit: Max items per page (defaults to :data:`PAGE_SIZE`).
    :returns: The :class:`HistoryPage`, oldest first.
    """
    items: list[TimelineItem] = [_exchange_item(ex, active_version) for ex in exchanges]
    items.extend(build_markers(records))
    items.sort(key=_sort_key)

    if before is not None:
        eligible = [item for item in items if item.cursor < before]
    else:
        eligible = items

    # Backward paging: take the newest ``limit`` of the eligible items, then
    # present them oldest first.
    page = eligible[-limit:] if limit > 0 else []
    has_more = len(eligible) > len(page)
    next_before = page[0].cursor if (page and has_more) else None

    return HistoryPage(
        items=[item.payload for item in page],
        next_before=next_before,
        has_more=has_more,
    )


def _sort_key(item: TimelineItem) -> tuple[str, int, str]:
    """Stable ordering key for a timeline item.

    Primary key is the shared timestamp cursor. On a tie, markers sort after
    Exchanges at the same instant (so a refresh that completes at the same
    timestamp as an Exchange appears below it), and within each kind a secondary
    key (version for markers, ``asked_at`` for Exchanges) keeps the order
    deterministic across calls — essential so backward paging never repeats or
    skips an item at a page boundary.
    """
    is_marker = item.payload.get("type") == MARKER_TYPE
    secondary = (
        str(item.payload.get("version", "")) if is_marker else str(item.payload.get("id", ""))
    )
    return (item.cursor, 1 if is_marker else 0, secondary)


# ---------------------------------------------------------------------------
# S3 / DB reads (the only impure part)
# ---------------------------------------------------------------------------


def _load_exchanges(dataset_id: str) -> list[dict[str, Any]]:
    """Read every saved Exchange document for *dataset_id* from S3.

    Lists the dataset's chat prefix (the same objects
    :func:`app.chat.assembly.load_recent_exchanges` reads) and parses each JSON
    object. The listing is chronological because the key's ISO-8601 timestamp
    prefix sorts lexicographically by time, but ordering is re-established by the
    merge regardless. The shared history includes every conversation's Exchanges
    (Requirement 5.6), so no conversation filter is applied here.
    """
    exchanges: list[dict[str, Any]] = []
    for key in s3.list_keys(keys.dataset_chat_prefix(dataset_id)):
        raw: dict[str, Any] = json.loads(s3.get_text(key))
        exchanges.append(raw)
    return exchanges


def _load_version_records(session: Session, dataset_id: str) -> list[VersionRecord]:
    """Read the ``dataset_versions`` rows for *dataset_id* as marker inputs.

    Reuses the ``dataset_versions`` model (dataset-ingestion) rather than
    re-implementing it. All versions are read (including version 1 and in-flight
    rows); :func:`build_markers` drops version 1 and :func:`_marker_state`
    classifies pending/completed/failed from ``outcome``/``completed_at``.
    """
    stmt = (
        select(DatasetVersion)
        .where(DatasetVersion.dataset_id == dataset_id)
        .order_by(DatasetVersion.version)
    )
    return [VersionRecord.from_row(row) for row in session.scalars(stmt).all()]


def load_history(
    dataset_id: str,
    *,
    before: str | None = None,
    limit: int = PAGE_SIZE,
) -> HistoryPage:
    """Load one page of the shared timeline for *dataset_id*.

    Reads the dataset's ``active_version`` and ``dataset_versions`` rows from the
    database (through :mod:`app.core.db`) and its saved Exchanges from S3, then
    delegates to the pure :func:`merge_timeline`. Returns the newest page when
    *before* is ``None``, or the page immediately older than *before* otherwise
    (Requirement 5.3). An unknown dataset id, or one with no history and no
    refreshes, yields an empty page rather than an error — the no-sign-in app
    never leaks whether an id exists, and an empty history is a valid state.
    """
    with session_scope() as session:
        dataset = session.get(Dataset, dataset_id)
        active_version = dataset.active_version if dataset is not None else None
        records = _load_version_records(session, dataset_id)

    exchanges = _load_exchanges(dataset_id)
    return merge_timeline(
        exchanges,
        records,
        active_version=active_version,
        before=before,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    """Return an ISO-8601 string for a timestamp column, or ``None``.

    Mirrors ``app.datasets.library._iso`` so marker timestamps serialize the
    same way the Library's version views do.
    """
    return value.isoformat() if value is not None else None
