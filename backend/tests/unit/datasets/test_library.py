"""Unit tests for app.datasets.library (dataset-library task 1).

These tests cover the pure, offline parts of the Library read layer:

- :func:`app.datasets.library.derive_display_state` for **every** row of the
  design's ``display_state`` table, plus the two combinations that cannot arise
  in practice, so the derivation is proven total (Requirement 2.5, Correctness
  Property 1).
- the row derivation helpers that turn a dataset + its versions into a
  :class:`~app.datasets.library.DatasetRow`: ``main_url`` for URL vs upload
  datasets, ``last_message`` from the event log, ``last_refreshed_at`` from the
  active version's ``completed_at``, ``review_count`` from ``metrics``, and
  ``refresh_check_id`` from ``status_detail`` (Requirement 2.1).
- the LIKE-escaping helper so a search term with wildcards matches literally
  (Correctness Property 3).

The filter / sort / search *combinations* against a real database are the
integration suite (``tests/integration/datasets/test_library_int.py``); here we
exercise the pieces that need no database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from app.datasets import library
from app.db.models import Dataset, DatasetStatus, DatasetVersion, SourceType

_DATASET_ID = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# display_state: every row of the design table (Property 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "active_version", "expected"),
    [
        # requested / processing + no active version -> processing
        (DatasetStatus.REQUESTED, None, "processing"),
        (DatasetStatus.PROCESSING, None, "processing"),
        # requested / processing + an active version -> ready_refreshing
        (DatasetStatus.REQUESTED, 1, "ready_refreshing"),
        (DatasetStatus.PROCESSING, 3, "ready_refreshing"),
        # updated + active version -> ready
        (DatasetStatus.UPDATED, 1, "ready"),
        (DatasetStatus.UPDATED, 5, "ready"),
        # failed + active version -> ready_refresh_failed (usable data kept)
        (DatasetStatus.FAILED, 2, "ready_refresh_failed"),
        # failed + no active version -> failed (no usable data)
        (DatasetStatus.FAILED, None, "failed"),
    ],
)
def test_display_state_matches_design_table(
    status: DatasetStatus, active_version: int | None, expected: str
) -> None:
    """Every row of the design's display_state table maps as specified.

    _Validates: Requirement 2.5 (design Correctness Property 1)._
    """
    assert library.derive_display_state(status, active_version) == expected


def test_display_state_is_total_for_string_status() -> None:
    """A raw string status (as returned by the DB) is accepted too."""
    assert library.derive_display_state("updated", 1) == "ready"
    assert library.derive_display_state("failed", None) == "failed"


def test_display_state_updated_without_active_version_is_safe() -> None:
    """`updated` with no active version (cannot occur) stays total -> processing."""
    assert library.derive_display_state(DatasetStatus.UPDATED, None) == "processing"


# ---------------------------------------------------------------------------
# Dataset / version builders (plain ORM instances; no DB)
# ---------------------------------------------------------------------------


def _dataset(
    *,
    status: DatasetStatus = DatasetStatus.UPDATED,
    source_type: SourceType = SourceType.URL,
    name: str = "Acme CRM",
    original_url: str | None = "https://g2.com/products/acme/reviews",
    active_version: int | None = 1,
    data_version: int = 1,
    status_detail: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    archived_at: datetime | None = None,
) -> Dataset:
    """Build a plain :class:`Dataset` ORM instance (not attached to a session)."""
    return Dataset(
        id=_DATASET_ID,
        name=name,
        source_type=source_type,
        original_url=original_url,
        final_url=original_url,
        normalized_url=original_url,
        platform="g2.com",
        status=status,
        status_detail=status_detail if status_detail is not None else {"events": []},
        requested_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 2, tzinfo=UTC),
        archived_at=archived_at,
        metrics=metrics,
        data_version=data_version,
        active_version=active_version,
    )


def _version(
    *,
    version: int,
    completed_at: datetime | None = None,
    review_count: int | None = None,
    outcome: str | None = None,
    trigger: str = "initial",
) -> DatasetVersion:
    return DatasetVersion(
        dataset_id=_DATASET_ID,
        version=version,
        trigger=trigger,
        requested_at=datetime(2024, 1, 1, tzinfo=UTC),
        completed_at=completed_at,
        review_count=review_count,
        extraction_method="ai" if outcome == "updated" else None,
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# main_url (Requirement 2.1)
# ---------------------------------------------------------------------------


def test_main_url_is_original_url_for_url_dataset() -> None:
    row = library._to_row(_dataset(source_type=SourceType.URL), [])
    assert row.main_url == "https://g2.com/products/acme/reviews"


def test_main_url_is_name_for_upload_dataset() -> None:
    """An upload dataset has no URL, so its main URL is the stored file name."""
    dataset = _dataset(source_type=SourceType.UPLOAD, name="acme-reviews.csv", original_url=None)
    row = library._to_row(dataset, [])
    assert row.main_url == "acme-reviews.csv"


# ---------------------------------------------------------------------------
# last_message (from the event log)
# ---------------------------------------------------------------------------


def test_last_message_is_the_most_recent_event_message() -> None:
    detail = {
        "events": [
            {"status": "requested", "message": "requested"},
            {"status": None, "message": "Fetching page 3 of 10"},
        ]
    }
    row = library._to_row(_dataset(status_detail=detail), [])
    assert row.last_message == "Fetching page 3 of 10"


def test_last_message_is_none_for_empty_event_log() -> None:
    row = library._to_row(_dataset(status_detail={"events": []}), [])
    assert row.last_message is None


# ---------------------------------------------------------------------------
# last_refreshed_at (active version's completed_at)
# ---------------------------------------------------------------------------


def test_last_refreshed_at_is_active_version_completed_at() -> None:
    completed = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    dataset = _dataset(active_version=2, data_version=2)
    versions = [
        _version(version=1, completed_at=datetime(2024, 1, 5, tzinfo=UTC), outcome="updated"),
        _version(version=2, completed_at=completed, outcome="updated"),
    ]
    row = library._to_row(dataset, versions)
    assert row.last_refreshed_at == completed.isoformat()


def test_last_refreshed_at_is_none_without_active_version() -> None:
    dataset = _dataset(status=DatasetStatus.REQUESTED, active_version=None)
    row = library._to_row(dataset, [_version(version=1)])
    assert row.last_refreshed_at is None


def test_last_refreshed_at_is_none_when_active_version_not_complete() -> None:
    """An active version whose row has no completed_at yields None, not a crash."""
    dataset = _dataset(active_version=1)
    row = library._to_row(dataset, [_version(version=1, completed_at=None)])
    assert row.last_refreshed_at is None


# ---------------------------------------------------------------------------
# review_count (from metrics)
# ---------------------------------------------------------------------------


def test_review_count_read_from_metrics() -> None:
    row = library._to_row(_dataset(metrics={"review_count": 212}), [])
    assert row.review_count == 212


def test_review_count_none_when_no_metrics() -> None:
    row = library._to_row(_dataset(metrics=None), [])
    assert row.review_count is None


# ---------------------------------------------------------------------------
# refresh_check_id (from status_detail)
# ---------------------------------------------------------------------------


def test_refresh_check_id_read_from_status_detail() -> None:
    detail = {"events": [], "refresh_check_id": "chk-123"}
    row = library._to_row(_dataset(status_detail=detail), [])
    assert row.refresh_check_id == "chk-123"


def test_refresh_check_id_none_when_absent() -> None:
    row = library._to_row(_dataset(status_detail={"events": []}), [])
    assert row.refresh_check_id is None


# ---------------------------------------------------------------------------
# archived_at surfaced on the row
# ---------------------------------------------------------------------------


def test_archived_at_surfaced_on_row() -> None:
    archived = datetime(2024, 4, 1, tzinfo=UTC)
    row = library._to_row(_dataset(archived_at=archived), [])
    assert row.archived_at == archived.isoformat()


# ---------------------------------------------------------------------------
# to_dict shape carries all design "API endpoints" fields
# ---------------------------------------------------------------------------


def test_row_to_dict_has_every_api_field() -> None:
    row = library._to_row(_dataset(), [_version(version=1, outcome="updated")])
    data = row.to_dict()
    assert set(data) == {
        "id",
        "name",
        "main_url",
        "platform",
        "status",
        "display_state",
        "last_message",
        "review_count",
        "data_version",
        "active_version",
        "requested_at",
        "last_refreshed_at",
        "archived_at",
        "refresh_check_id",
    }


# ---------------------------------------------------------------------------
# upload description (sourced from status_detail; task 9)
# ---------------------------------------------------------------------------


def test_upload_description_read_from_status_detail() -> None:
    """An upload's source description is read back from status_detail."""
    detail = {"events": [], "description": "Q1 support tickets"}
    dataset = _dataset(source_type=SourceType.UPLOAD, status_detail=detail)
    assert library._upload_description(dataset) == "Q1 support tickets"


def test_upload_description_none_when_absent() -> None:
    dataset = _dataset(source_type=SourceType.UPLOAD, status_detail={"events": []})
    assert library._upload_description(dataset) is None


def test_upload_description_none_for_url_dataset() -> None:
    """URL datasets never set a description, so it is None."""
    dataset = _dataset(source_type=SourceType.URL, status_detail={"events": []})
    assert library._upload_description(dataset) is None


def test_upload_description_none_when_empty_string() -> None:
    """A blank stored description surfaces as None, not ``""``."""
    dataset = _dataset(source_type=SourceType.UPLOAD, status_detail={"description": ""})
    assert library._upload_description(dataset) is None


# ---------------------------------------------------------------------------
# DatasetDetail.to_dict carries status_detail + description (task 9)
# ---------------------------------------------------------------------------


def _detail(
    *,
    status_detail: dict[str, Any],
    description: str | None,
    metrics: dict[str, Any] | None = None,
) -> library.DatasetDetail:
    """Build a DatasetDetail directly (no DB) for the to_dict shape tests."""
    dataset = _dataset(status_detail=status_detail, metrics=metrics)
    row = library._to_row(dataset, [])
    return library.DatasetDetail(
        row=row,
        source_type=dataset.source_type.value,
        original_url=dataset.original_url,
        final_url=dataset.final_url,
        page_title=dataset.page_title,
        metrics=dataset.metrics,
        status_detail=status_detail,
        description=description,
        versions=[],
    )


def test_detail_to_dict_includes_status_detail_and_description() -> None:
    """The detail record carries status_detail (events/redirects/viability) and
    the upload description, in addition to every list field (task 9,
    Requirements 3.1, 6.4)."""
    status_detail = {
        "events": [
            {"status": "requested", "at": "2024-01-01T00:00:00+00:00", "message": "requested"},
            {"status": "updated", "at": "2024-01-02T00:00:00+00:00", "message": "done"},
        ],
        "redirects": [
            {"from": "http://example.com", "to": "https://example.com", "status": 301},
        ],
        "viability": {"verdict": "will_work", "reasons": [], "warnings": [], "evidence": {}},
        "refresh_check_id": None,
    }
    data = _detail(status_detail=status_detail, description="Support tickets export").to_dict()

    # New detail-only fields are present and passed through unreshaped.
    assert data["status_detail"] == status_detail
    assert data["status_detail"]["events"][-1]["message"] == "done"
    assert data["status_detail"]["redirects"][0]["status"] == 301
    assert data["status_detail"]["viability"]["verdict"] == "will_work"
    assert data["description"] == "Support tickets export"

    # Still carries every list-row field + the existing detail fields.
    for key in (
        "id",
        "name",
        "main_url",
        "status",
        "display_state",
        "source_type",
        "original_url",
        "final_url",
        "page_title",
        "metrics",
        "versions",
    ):
        assert key in data


def test_detail_to_dict_metrics_left_unchanged_no_entity() -> None:
    """metrics is returned as-is: the entity profile is NOT merged in (it lives
    in reviews/v{n}.json, read by EntityCard)."""
    metrics = {"review_count": 42, "avg_rating": 4.1}
    data = _detail(status_detail={"events": []}, description=None, metrics=metrics).to_dict()
    assert data["metrics"] == metrics
    assert "entity" not in data["metrics"]
    assert data["description"] is None


# ---------------------------------------------------------------------------
# LIKE escaping (Property 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("acme", "acme"),
        ("50%", "50\\%"),
        ("a_b", "a\\_b"),
        ("back\\slash", "back\\\\slash"),
        ("%_\\", "\\%\\_\\\\"),
    ],
)
def test_escape_like_escapes_wildcards(raw: str, expected: str) -> None:
    """LIKE wildcards and the escape char are escaped so matches are literal.

    _Validates: Requirement 2.3 (design Correctness Property 3)._
    """
    assert library._escape_like(raw) == expected
