"""Property-based test for the chat history timeline merge (guardrailed-chat Task 10).

Property 3: Timeline is ordered and complete.
  For any set of Exchanges and version records, the merged timeline SHALL be in
  time order, SHALL contain one marker per version after the first, and SHALL
  flag exactly the Exchanges from older versions as stale.
  Validates: Requirements 5.2, 9.1, 9.4

Drives the REAL :func:`app.chat.history.merge_timeline` (and, through it,
:func:`app.chat.history.build_markers`) over generated Exchanges (varied
``data_version`` / ``asked_at``) and :class:`VersionRecord`s. By passing
``before=None`` and a ``limit`` large enough to cover every item, the whole
merged timeline is returned in one page, so the property can check:

  * the page is in non-decreasing timeline (cursor) order, oldest first;
  * there is exactly one marker per version > 1 that carries a usable timestamp;
  * an Exchange is flagged ``is_stale`` exactly when its ``data_version`` is
    older than the dataset's ``active_version``.

An independent oracle re-derives the expected markers and stale set.
"""

from __future__ import annotations

from typing import Any

from app.chat.history import (
    EXCHANGE_TYPE,
    MARKER_TYPE,
    VersionRecord,
    merge_timeline,
)
from hypothesis import given
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Sortable timestamp strings whose lexicographic order equals chronological
# order (so cursor ordering is well-defined and ties can occur).
_ts = st.integers(min_value=0, max_value=60).map(lambda n: f"2026-01-01T00:00:{n:02d}Z")

_version = st.integers(min_value=1, max_value=5)
_trigger = st.sampled_from(["manual_refresh", "duplicate_submission", "upload_replace"])
_outcome = st.sampled_from(["updated", "failed", None])


@st.composite
def _exchange(draw: st.DrawFn) -> dict[str, Any]:
    """A saved Exchange document, reduced to the fields the merge reads."""
    return {
        "id": draw(st.text(alphabet="0123456789abcdef", min_size=4, max_size=8)),
        "data_version": draw(_version),
        "asked_at": draw(_ts),
        "question": "q",
        "answer": "a",
    }


@st.composite
def _version_record(draw: st.DrawFn) -> VersionRecord:
    """A dataset_versions view; a terminal row has completed_at, pending doesn't."""
    outcome = draw(_outcome)
    requested_at = draw(_ts)
    # Terminal rows carry a completed_at; pending (outcome None) usually doesn't.
    completed_at = draw(st.one_of(st.none(), _ts)) if outcome is not None else None
    return VersionRecord(
        version=draw(_version),
        trigger=draw(_trigger),
        requested_at=requested_at,
        completed_at=completed_at,
        review_count=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=500))),
        outcome=outcome,
    )


def _marker_cursor(record: VersionRecord) -> str | None:
    """Oracle for the timestamp a marker sits at (mirrors history._marker_cursor).

    Terminal markers sit at completed_at (falling back to requested_at); pending
    markers sit at requested_at (falling back to completed_at). ``None`` means
    the row has no usable timestamp and produces no marker.
    """
    is_terminal = record.outcome == "failed" or (
        record.outcome == "updated" and record.completed_at is not None
    )
    if is_terminal:
        return record.completed_at or record.requested_at
    return record.requested_at or record.completed_at


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


@given(
    exchanges=st.lists(_exchange(), min_size=0, max_size=12),
    records=st.lists(_version_record(), min_size=0, max_size=6, unique_by=lambda r: r.version),
    active_version=st.one_of(st.none(), _version),
)
def test_timeline_is_ordered_complete_and_stale_flagged(
    exchanges: list[dict[str, Any]],
    records: list[VersionRecord],
    active_version: int | None,
) -> None:
    """Property 3: Timeline is ordered and complete.

    The full merged timeline is time-ordered, has exactly one marker per version
    > 1 (with a usable timestamp), and flags an Exchange stale exactly when its
    data version is older than the active version.
    Validates: Requirements 5.2, 9.1, 9.4
    """
    # A limit covering every possible item, newest page (before=None), so the
    # whole timeline is in one page.
    big_limit = len(exchanges) + len(records) + 10
    page = merge_timeline(
        exchanges,
        records,
        active_version=active_version,
        before=None,
        limit=big_limit,
    )
    items = page.items

    # The whole timeline fits in one page: nothing paged away.
    assert page.has_more is False
    assert page.next_before is None

    # 1) Time order (oldest first): cursors are non-decreasing. The payload has
    #    no cursor field, so re-derive each row's timeline timestamp.
    cursors = [_item_cursor(item) for item in items]
    assert cursors == sorted(cursors)

    # 2) Exactly one marker per version > 1 that has a usable timestamp.
    expected_marker_versions = sorted(
        r.version for r in records if r.version > 1 and _marker_cursor(r) is not None
    )
    actual_marker_versions = sorted(
        int(item["version"]) for item in items if item["type"] == MARKER_TYPE
    )
    assert actual_marker_versions == expected_marker_versions
    # One marker per such version (no duplicates, none for version 1).
    assert len(actual_marker_versions) == len(set(actual_marker_versions))
    assert all(v > 1 for v in actual_marker_versions)

    # 3) Every Exchange appears exactly once and carries the right is_stale flag.
    exchange_items = [item for item in items if item["type"] == EXCHANGE_TYPE]
    assert len(exchange_items) == len(exchanges)
    for item in exchange_items:
        dv = int(item["data_version"])
        expected_stale = active_version is not None and dv < active_version
        assert item["is_stale"] is expected_stale


def _item_cursor(item: dict[str, Any]) -> str:
    """Re-derive the timeline timestamp a serialized item was placed at.

    An Exchange sits at ``asked_at``; a marker at its completed_at or
    requested_at, matching :func:`_marker_cursor`.
    """
    if item["type"] == EXCHANGE_TYPE:
        return str(item.get("asked_at", ""))
    # Reconstruct the marker's VersionRecord-equivalent to reuse the oracle.
    record = VersionRecord(
        version=int(item["version"]),
        trigger=str(item["trigger"]),
        requested_at=item["requested_at"],
        completed_at=item["completed_at"],
        review_count=item["review_count"],
        outcome=_outcome_from_state(str(item["state"])),
    )
    cursor = _marker_cursor(record)
    assert cursor is not None  # markers with no cursor are never emitted
    return cursor


def _outcome_from_state(state: str) -> str | None:
    """Map a marker ``state`` back to the version ``outcome`` that produced it."""
    if state == "completed":
        return "updated"
    if state == "failed":
        return "failed"
    return None
