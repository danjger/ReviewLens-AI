"""Unit tests for the shared Q&A history timeline (``app.chat.history``), Task 5.1.

Covers the design's "Refresh markers" / "Endpoints" rules and Correctness
Property 3 ("Timeline is ordered and complete"):

- **Merge order**: Exchanges and refresh markers interleave strictly in time
  order, oldest first (Requirement 5.2).
- **Marker states**: a version row becomes a ``completed`` / ``pending`` /
  ``failed`` marker from its ``outcome``/``completed_at`` (Requirements 9.1,
  9.2, 9.3), placed at ``completed_at`` (terminal) or ``requested_at``
  (pending).
- **Stale flags**: exactly the Exchanges from versions older than
  ``active_version`` are flagged ``is_stale`` (Requirement 9.4).
- **Paging across markers**: backward paging by the shared ``before`` cursor
  pages markers and Exchanges together, including a boundary that falls between
  a marker and an Exchange (Requirement 5.3).
- **No marker for a URL-check failure**: a refresh that never created a version
  row produces no marker (Requirement 9.8).

Plus the thin reader (:func:`app.chat.history.load_history`) with S3 and the
database faked at the module boundary — integration against LocalStack is Task
5.4.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.chat import history
from app.chat.history import (
    MARKER_COMPLETED,
    MARKER_FAILED,
    MARKER_PENDING,
    PAGE_SIZE,
    VersionRecord,
    merge_timeline,
)

_DS = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _exchange(
    *,
    exchange_id: str,
    asked_at: str,
    data_version: int = 1,
    conversation_id: str = "conv-a",
) -> dict[str, Any]:
    """A saved Exchange document, reduced to the fields the timeline uses."""
    return {
        "id": exchange_id,
        "dataset_id": _DS,
        "data_version": data_version,
        "conversation_id": conversation_id,
        "asked_at": asked_at,
        "answered_at": asked_at,
        "question": f"q-{exchange_id}",
        "answer": f"a-{exchange_id}",
        "citations": [],
        "citation_snippets": {},
        "scope": "in_scope",
    }


def _version(
    *,
    version: int,
    trigger: str = "manual_refresh",
    requested_at: str | None = None,
    completed_at: str | None = None,
    review_count: int | None = None,
    outcome: str | None = None,
) -> VersionRecord:
    return VersionRecord(
        version=version,
        trigger=trigger,
        requested_at=requested_at,
        completed_at=completed_at,
        review_count=review_count,
        outcome=outcome,
    )


def _markers(page: history.HistoryPage) -> list[dict[str, Any]]:
    return [i for i in page.items if i["type"] == history.MARKER_TYPE]


def _exchanges(page: history.HistoryPage) -> list[dict[str, Any]]:
    return [i for i in page.items if i["type"] == history.EXCHANGE_TYPE]


# ---------------------------------------------------------------------------
# Merge order (Requirement 5.2 / Property 3)
# ---------------------------------------------------------------------------


def test_timeline_is_time_ordered_oldest_first() -> None:
    """Exchanges and markers interleave strictly by timestamp, oldest first."""
    exchanges = [
        _exchange(exchange_id="e1", asked_at="2026-01-01T00:00:00Z", data_version=1),
        _exchange(exchange_id="e2", asked_at="2026-01-03T00:00:00Z", data_version=2),
    ]
    records = [
        _version(version=1, trigger="add", requested_at="2026-01-01T00:00:00Z"),
        _version(
            version=2,
            requested_at="2026-01-02T00:00:00Z",
            completed_at="2026-01-02T12:00:00Z",
            review_count=180,
            outcome="updated",
        ),
    ]
    page = merge_timeline(exchanges, records, active_version=2)

    cursors = [i.get("asked_at") or i.get("completed_at") for i in page.items]
    assert cursors == sorted(cursors)
    # e1 (01), marker v2 (02-12:00), e2 (03): the marker sits between the two.
    kinds = [i["type"] for i in page.items]
    assert kinds == ["exchange", "refresh_marker", "exchange"]


def test_shared_history_includes_every_conversation() -> None:
    """The timeline contains Exchanges from every conversation (Requirement 5.6)."""
    exchanges = [
        _exchange(exchange_id="e1", asked_at="2026-01-01T00:00:00Z", conversation_id="conv-a"),
        _exchange(exchange_id="e2", asked_at="2026-01-02T00:00:00Z", conversation_id="conv-b"),
    ]
    page = merge_timeline(exchanges, [], active_version=1)
    assert {i["id"] for i in _exchanges(page)} == {"e1", "e2"}


# ---------------------------------------------------------------------------
# One marker per version after the first (Property 3 / Requirement 9.8)
# ---------------------------------------------------------------------------


def test_version_one_never_produces_a_marker() -> None:
    """The initial version is not a refresh, so it yields no marker."""
    records = [_version(version=1, trigger="add", completed_at="2026-01-01T00:00:00Z")]
    page = merge_timeline([], records, active_version=1)
    assert _markers(page) == []


def test_one_marker_per_version_after_the_first() -> None:
    """Each version > 1 contributes exactly one marker."""
    records = [
        _version(version=1, trigger="add"),
        _version(version=2, completed_at="2026-01-02T00:00:00Z", outcome="updated"),
        _version(version=3, completed_at="2026-01-03T00:00:00Z", outcome="updated"),
    ]
    page = merge_timeline([], records, active_version=3)
    assert [m["version"] for m in _markers(page)] == [2, 3]


def test_no_marker_for_refresh_that_failed_at_url_check() -> None:
    """A refresh failing at the URL check creates no version row → no marker.

    Requirement 9.8: when the page couldn't be read the data didn't change, so
    ``dataset_versions`` has no new row. Here only version 1 exists even though a
    refresh was attempted, so the timeline shows no refresh marker.
    """
    records = [_version(version=1, trigger="add", completed_at="2026-01-01T00:00:00Z")]
    page = merge_timeline(
        [_exchange(exchange_id="e1", asked_at="2026-01-05T00:00:00Z")],
        records,
        active_version=1,
    )
    assert _markers(page) == []
    assert [e["id"] for e in _exchanges(page)] == ["e1"]


# ---------------------------------------------------------------------------
# Marker states (Requirements 9.1, 9.2, 9.3)
# ---------------------------------------------------------------------------


def test_completed_marker_shape_and_position() -> None:
    """A finished version is a completed marker at completed_at with before/after counts."""
    records = [
        _version(version=1, trigger="add", review_count=180),
        _version(
            version=2,
            trigger="manual_refresh",
            requested_at="2026-01-02T00:00:00Z",
            completed_at="2026-01-02T12:00:00Z",
            review_count=212,
            outcome="updated",
        ),
    ]
    page = merge_timeline([], records, active_version=2)
    marker = _markers(page)[0]
    assert marker["state"] == MARKER_COMPLETED
    assert marker["version"] == 2
    assert marker["trigger"] == "manual_refresh"
    assert marker["review_count"] == 212
    assert marker["previous_review_count"] == 180
    assert marker["completed_at"] == "2026-01-02T12:00:00Z"


def test_pending_marker_sits_at_requested_at() -> None:
    """An in-flight version (no outcome) is a pending marker at requested_at."""
    records = [
        _version(version=1, trigger="add"),
        _version(
            version=2,
            requested_at="2026-01-02T00:00:00Z",
            completed_at=None,
            outcome=None,
        ),
    ]
    page = merge_timeline([], records, active_version=1)
    marker = _markers(page)[0]
    assert marker["state"] == MARKER_PENDING
    assert marker["completed_at"] is None
    # Placed at requested_at so it sorts to the end while the refresh runs.
    assert page.items[-1]["version"] == 2


def test_failed_marker_from_failed_outcome() -> None:
    """A failed refresh is a failed marker (answers still reflect the prior version)."""
    records = [
        _version(version=1, trigger="add"),
        _version(
            version=2,
            requested_at="2026-01-02T00:00:00Z",
            completed_at="2026-01-02T06:00:00Z",
            outcome="failed",
        ),
    ]
    page = merge_timeline([], records, active_version=1)
    marker = _markers(page)[0]
    assert marker["state"] == MARKER_FAILED


def test_pending_marker_becomes_completed_when_version_finishes() -> None:
    """The same version row flips pending → completed once it has a terminal outcome."""
    pending = _version(version=2, requested_at="2026-01-02T00:00:00Z", outcome=None)
    assert merge_timeline([], [pending], active_version=1).items[0]["state"] == MARKER_PENDING

    completed = _version(
        version=2,
        requested_at="2026-01-02T00:00:00Z",
        completed_at="2026-01-02T12:00:00Z",
        review_count=212,
        outcome="updated",
    )
    assert merge_timeline([], [completed], active_version=2).items[0]["state"] == MARKER_COMPLETED


# ---------------------------------------------------------------------------
# Stale flags (Requirement 9.4 / Property 3)
# ---------------------------------------------------------------------------


def test_exactly_older_version_exchanges_are_stale() -> None:
    """Only Exchanges from versions older than active_version are flagged stale."""
    exchanges = [
        _exchange(exchange_id="v1", asked_at="2026-01-01T00:00:00Z", data_version=1),
        _exchange(exchange_id="v2", asked_at="2026-01-02T00:00:00Z", data_version=2),
        _exchange(exchange_id="v3", asked_at="2026-01-03T00:00:00Z", data_version=3),
    ]
    page = merge_timeline(exchanges, [], active_version=3)
    stale = {e["id"]: e["is_stale"] for e in _exchanges(page)}
    assert stale == {"v1": True, "v2": True, "v3": False}


def test_nothing_stale_without_active_version() -> None:
    """With no active_version (first processing unfinished) nothing is stale."""
    exchanges = [_exchange(exchange_id="e1", asked_at="2026-01-01T00:00:00Z", data_version=1)]
    page = merge_timeline(exchanges, [], active_version=None)
    assert _exchanges(page)[0]["is_stale"] is False


# ---------------------------------------------------------------------------
# Backward paging on the shared cursor (Requirement 5.3)
# ---------------------------------------------------------------------------


def test_newest_page_returns_last_limit_items_oldest_first() -> None:
    """Without a cursor the page is the newest ``limit`` items, oldest first."""
    exchanges = [
        _exchange(exchange_id=f"e{i}", asked_at=f"2026-01-{i:02d}T00:00:00Z")
        for i in range(1, 6)  # days 01..05
    ]
    page = merge_timeline(exchanges, [], active_version=1, limit=2)
    assert [e["id"] for e in page.items] == ["e4", "e5"]
    assert page.has_more is True
    assert page.next_before == "2026-01-04T00:00:00Z"


def test_before_cursor_fetches_the_older_page() -> None:
    """Passing next_before returns the immediately older page, with no overlap."""
    exchanges = [
        _exchange(exchange_id=f"e{i}", asked_at=f"2026-01-{i:02d}T00:00:00Z") for i in range(1, 6)
    ]
    first = merge_timeline(exchanges, [], active_version=1, limit=2)
    second = merge_timeline(exchanges, [], active_version=1, limit=2, before=first.next_before)
    assert [e["id"] for e in second.items] == ["e2", "e3"]
    assert second.has_more is True
    assert second.next_before == "2026-01-02T00:00:00Z"

    third = merge_timeline(exchanges, [], active_version=1, limit=2, before=second.next_before)
    assert [e["id"] for e in third.items] == ["e1"]
    assert third.has_more is False
    assert third.next_before is None


def test_paging_boundary_between_marker_and_exchange() -> None:
    """A page boundary that falls between a marker and an Exchange pages cleanly.

    The timeline is: e1 (01), marker v2 (02-12:00), e2 (03). With a page size of
    1 the first page is e2, the next is the v2 marker, and the last is e1 — the
    shared cursor steps across the marker/Exchange boundary without dropping or
    repeating either.
    """
    exchanges = [
        _exchange(exchange_id="e1", asked_at="2026-01-01T00:00:00Z", data_version=1),
        _exchange(exchange_id="e2", asked_at="2026-01-03T00:00:00Z", data_version=2),
    ]
    records = [
        _version(version=1, trigger="add"),
        _version(
            version=2,
            requested_at="2026-01-02T00:00:00Z",
            completed_at="2026-01-02T12:00:00Z",
            review_count=180,
            outcome="updated",
        ),
    ]
    p1 = merge_timeline(exchanges, records, active_version=2, limit=1)
    assert [i["type"] for i in p1.items] == ["exchange"]
    assert p1.items[0]["id"] == "e2"

    p2 = merge_timeline(exchanges, records, active_version=2, limit=1, before=p1.next_before)
    assert [i["type"] for i in p2.items] == ["refresh_marker"]
    assert p2.items[0]["version"] == 2

    p3 = merge_timeline(exchanges, records, active_version=2, limit=1, before=p2.next_before)
    assert [i["type"] for i in p3.items] == ["exchange"]
    assert p3.items[0]["id"] == "e1"
    assert p3.has_more is False
    assert p3.next_before is None


def test_default_page_size_is_twenty() -> None:
    """The default limit matches Requirement 5.3's page size of 20."""
    assert PAGE_SIZE == 20
    exchanges = [
        _exchange(exchange_id=f"e{i}", asked_at=f"2026-01-01T00:00:{i:02d}Z") for i in range(25)
    ]
    page = merge_timeline(exchanges, [], active_version=1)
    assert len(page.items) == 20
    assert page.has_more is True


# ---------------------------------------------------------------------------
# load_history reader (S3 + DB faked at the module boundary)
# ---------------------------------------------------------------------------


class _FakeVersionRow:
    """Minimal stand-in for a DatasetVersion ORM row for VersionRecord.from_row."""

    def __init__(self, record: VersionRecord) -> None:
        self.version = record.version
        self.trigger = record.trigger
        self.requested_at = _FakeTs(record.requested_at)
        self.completed_at = _FakeTs(record.completed_at) if record.completed_at else None
        self.review_count = record.review_count
        self.outcome = record.outcome


class _FakeTs:
    """A timestamp whose ``isoformat`` returns a preset ISO string."""

    def __init__(self, iso: str | None) -> None:
        self._iso = iso

    def isoformat(self) -> str | None:
        return self._iso


def test_load_history_merges_s3_exchanges_and_db_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_history reads Exchanges from S3 and versions from the DB, then merges."""
    objects = {
        history.keys.dataset_chat_exchange(_DS, "2026-01-01T00:00:00Z", "e1"): json.dumps(
            _exchange(exchange_id="e1", asked_at="2026-01-01T00:00:00Z", data_version=1)
        ),
        history.keys.dataset_chat_exchange(_DS, "2026-01-03T00:00:00Z", "e2"): json.dumps(
            _exchange(exchange_id="e2", asked_at="2026-01-03T00:00:00Z", data_version=2)
        ),
    }

    def _list_keys(prefix: str, *, start_after: str = "") -> list[str]:
        return sorted(k for k in objects if k.startswith(prefix))

    def _get_text(key: str, *, encoding: str = "utf-8") -> str:
        return objects[key]

    monkeypatch.setattr(history.s3, "list_keys", _list_keys)
    monkeypatch.setattr(history.s3, "get_text", _get_text)

    rows = [
        _FakeVersionRow(_version(version=1, trigger="add")),
        _FakeVersionRow(
            _version(
                version=2,
                requested_at="2026-01-02T00:00:00Z",
                completed_at="2026-01-02T12:00:00Z",
                review_count=212,
                outcome="updated",
            )
        ),
    ]
    monkeypatch.setattr(
        history,
        "_load_version_records",
        lambda session, dataset_id: [VersionRecord.from_row(r) for r in rows],
    )

    class _FakeDataset:
        active_version = 2

    class _FakeSession:
        def get(self, model: object, key: str) -> _FakeDataset:
            return _FakeDataset()

    class _FakeScope:
        def __enter__(self) -> _FakeSession:
            return _FakeSession()

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(history, "session_scope", lambda: _FakeScope())

    page = history.load_history(_DS)
    assert [i["type"] for i in page.items] == ["exchange", "refresh_marker", "exchange"]
    # e1 is on v1 < active 2 → stale; e2 is on v2 == active → fresh.
    by_id = {i["id"]: i for i in _exchanges(page)}
    assert by_id["e1"]["is_stale"] is True
    assert by_id["e2"]["is_stale"] is False


def test_load_history_unknown_dataset_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown id (no dataset row, no objects) yields an empty page, not an error."""
    monkeypatch.setattr(history.s3, "list_keys", lambda prefix, **kw: [])

    class _FakeSession:
        def get(self, model: object, key: str) -> None:
            return None

    class _FakeScope:
        def __enter__(self) -> _FakeSession:
            return _FakeSession()

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(history, "session_scope", lambda: _FakeScope())
    monkeypatch.setattr(history, "_load_version_records", lambda session, dataset_id: [])

    page = history.load_history("does-not-exist")
    assert page.items == []
    assert page.has_more is False
    assert page.next_before is None
