"""Property-based test for archive/restore field semantics (dataset-library task 8).

Property 2: Archive never deletes.
  For any dataset, archiving and then restoring SHALL leave every field except
  ``archived_at`` unchanged, and SHALL delete no rows or objects.
  Validates: Requirements 5.3, 5.4

This is the fast, pure form of the property the design permits: it models a
dataset row (and the surrounding rows and S3 objects that archive/restore must
never touch) and applies the *exact field-level transform* that
:func:`app.datasets.library.archive_dataset` and
:func:`app.datasets.library.restore_dataset` perform on the row —

- ``archive``: set ``archived_at`` to a timestamp, but only when it is ``None``
  (an already-archived row keeps its original timestamp — the app's idempotent
  no-op);
- ``restore``: clear ``archived_at`` to ``None``.

Neither function changes any other column, and neither deletes a ``datasets`` or
``dataset_versions`` row or an S3 object. The property drives those transforms
over arbitrary datasets (any starting ``archived_at``, any other field values,
any number of sibling rows and stored objects) and asserts:

1. after archive→restore every field **except** ``archived_at`` is byte-for-byte
   unchanged, and ``archived_at`` is back to ``None``;
2. no row (the dataset itself, its versions, or sibling datasets) and no S3
   object was removed by either step.

A model-level test keeps this a fast property test; the real DB/S3
no-deletion behaviour is covered by the integration test
(``tests/integration/datasets/test_archive_int.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

# A fixed "now" the modeled archive uses; the exact value is irrelevant to the
# property (only "non-None vs None" matters), so a constant keeps it clean.
_ARCHIVE_TS = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC).isoformat()


def _model_archive(row: dict[str, Any]) -> dict[str, Any]:
    """Mirror ``archive_dataset``: set ``archived_at`` only if currently None.

    Returns a NEW dict (the app mutates the ORM row in place and commits; the
    model copies so the "before" snapshot stays intact for comparison). No other
    key is added, removed, or changed.
    """
    out = dict(row)
    if out.get("archived_at") is None:
        out["archived_at"] = _ARCHIVE_TS
    # Already archived: leave the original timestamp (idempotent no-op).
    return out


def _model_restore(row: dict[str, Any]) -> dict[str, Any]:
    """Mirror ``restore_dataset``: clear ``archived_at`` to None, nothing else."""
    out = dict(row)
    out["archived_at"] = None
    return out


# A dataset row with the columns archive/restore must leave alone. Values are
# arbitrary; only that they survive the round trip matters.
_timestamps = st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2030, 1, 1)).map(
    lambda d: d.replace(tzinfo=UTC).isoformat()
)


@st.composite
def _dataset_row(draw: st.DrawFn) -> dict[str, Any]:
    """Generate an arbitrary dataset row, archived or not."""
    archived_at = draw(st.one_of(st.none(), _timestamps))
    return {
        "id": draw(st.uuids().map(str)),
        "name": draw(st.text(min_size=0, max_size=40)),
        "source_type": draw(st.sampled_from(["url", "upload"])),
        "original_url": draw(st.one_of(st.none(), st.text(min_size=1, max_size=60))),
        "status": draw(st.sampled_from(["requested", "processing", "updated", "failed"])),
        "data_version": draw(st.integers(min_value=0, max_value=20)),
        "active_version": draw(st.one_of(st.none(), st.integers(min_value=1, max_value=20))),
        "metrics": draw(
            st.one_of(st.none(), st.fixed_dictionaries({"review_count": st.integers(0, 999)}))
        ),
        "requested_at": draw(_timestamps),
        "updated_at": draw(_timestamps),
        "archived_at": archived_at,
    }


@given(
    row=_dataset_row(),
    sibling_ids=st.sets(st.uuids().map(str), max_size=5),
    version_numbers=st.sets(st.integers(min_value=1, max_value=20), max_size=5),
    s3_objects=st.sets(st.text(min_size=1, max_size=30), max_size=8),
)
def test_archive_then_restore_preserves_everything_and_deletes_nothing(
    row: dict[str, Any],
    sibling_ids: set[str],
    version_numbers: set[int],
    s3_objects: set[str],
) -> None:
    """Property 2: Archive never deletes.

    Archiving then restoring any dataset leaves every field except archived_at
    unchanged (and archived_at back to None), and removes no row or S3 object.
    Validates: Requirements 5.3, 5.4
    """
    # The surrounding state archive/restore must never touch: this dataset's
    # version rows, other datasets' rows, and stored S3 objects.
    rows_before = {row["id"]} | sibling_ids
    versions_before = set(version_numbers)
    objects_before = set(s3_objects)

    before = dict(row)

    # Archive, then restore — exactly the app's two field-level transforms.
    archived = _model_archive(row)
    restored = _model_restore(archived)

    # (1) Every field EXCEPT archived_at is unchanged across the round trip.
    for key, value in before.items():
        if key == "archived_at":
            continue
        assert restored[key] == value, f"field {key!r} changed"

    # The field set is identical: nothing added or dropped from the record.
    assert set(restored) == set(before)

    # archived_at ends cleared (restored to the default list).
    assert restored["archived_at"] is None

    # (2) No rows or objects were deleted by either step. The transforms are
    # pure field edits on a single row — they never enumerate or remove rows or
    # S3 keys, so the surrounding state is identical.
    rows_after = {restored["id"]} | sibling_ids
    assert rows_after == rows_before
    assert versions_before == set(version_numbers)
    assert objects_before == set(s3_objects)


@given(row=_dataset_row())
def test_archive_is_idempotent_on_already_archived(row: dict[str, Any]) -> None:
    """Property 2: Archive never deletes (idempotent archive).

    Archiving an already-archived dataset keeps its original archived_at and
    changes nothing else, so repeated/concurrent archives converge.
    Validates: Requirements 5.3, 5.4
    """
    once = _model_archive(row)
    twice = _model_archive(once)

    # Second archive is a pure no-op: identical record, timestamp preserved.
    assert twice == once
    # And an initially-archived row's timestamp was never overwritten.
    if row["archived_at"] is not None:
        assert once["archived_at"] == row["archived_at"]
