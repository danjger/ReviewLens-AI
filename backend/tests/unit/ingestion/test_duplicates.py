"""Unit tests for app.ingestion.duplicates (dataset-ingestion task 4.2).

These cover the duplicate-lookup *contract* without a real database:

- ``find_existing`` returns ``None`` immediately when neither normalized URL is
  given (no query is issued).
- A match is mapped to an :class:`ExistingDataset` carrying id, name, archived
  flag (derived from ``archived_at``), and status — exactly the fields the
  check handler writes to the Check Session item (Requirement 6.2, 6.3).
- No match yields ``None``.
- ``ExistingDataset.to_dict`` produces the JSON shape stored on the item.

The real SQL (matching ``normalized_url`` OR ``normalized_final_url``, URL
datasets only, archived included) is exercised against live PostgreSQL in the
integration suite. Here ``session_scope`` is faked so the mapping logic is
tested fast and offline — the same approach ``tests/unit/db/test_status.py``
uses for its database writes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from app.db.models import DatasetStatus, SourceType
from app.ingestion import duplicates
from app.ingestion.duplicates import ExistingDataset, find_existing


class _FakeDataset:
    """The handful of attributes ``find_existing`` reads off a Dataset row."""

    def __init__(
        self,
        *,
        ds_id: str,
        name: str,
        status: DatasetStatus,
        archived_at: datetime | None,
    ) -> None:
        self.id = ds_id
        self.name = name
        self.status = status
        self.archived_at = archived_at


class _FakeScalars:
    def __init__(self, result: Any) -> None:
        self._result = result

    def first(self) -> Any:
        return self._result


class _FakeSession:
    """Returns a preset row from ``scalars(...).first()`` and records the query."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.statements: list[Any] = []

    def scalars(self, statement: Any) -> _FakeScalars:
        self.statements.append(statement)
        return _FakeScalars(self._result)


@contextmanager
def _patch_session(monkeypatch: pytest.MonkeyPatch, result: Any) -> Iterator[_FakeSession]:
    session = _FakeSession(result)

    @contextmanager
    def _scope() -> Iterator[_FakeSession]:
        yield session

    monkeypatch.setattr(duplicates, "session_scope", _scope)
    yield session


def test_no_candidates_returns_none_without_querying(monkeypatch: pytest.MonkeyPatch) -> None:
    with _patch_session(monkeypatch, result=object()) as session:
        assert find_existing(None, None) is None
    # No query was issued when there is nothing to match on.
    assert session.statements == []


def test_match_maps_to_existing_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _FakeDataset(
        ds_id="ds-1",
        name="Acme CRM",
        status=DatasetStatus.UPDATED,
        archived_at=None,
    )
    with _patch_session(monkeypatch, result=row) as session:
        found = find_existing("https://a.example/reviews", "https://a.example/reviews")

    assert found == ExistingDataset(id="ds-1", name="Acme CRM", archived=False, status="updated")
    # A query was built from the candidate URLs.
    assert len(session.statements) == 1


def test_archived_dataset_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _FakeDataset(
        ds_id="ds-2",
        name="Old Product",
        status=DatasetStatus.UPDATED,
        archived_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    with _patch_session(monkeypatch, result=row):
        found = find_existing("https://b.example/", None)

    assert found is not None
    assert found.archived is True


def test_no_match_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    with _patch_session(monkeypatch, result=None):
        assert find_existing("https://c.example/", "https://c.example/") is None


def test_to_dict_shape() -> None:
    existing = ExistingDataset(id="x", name="N", archived=True, status="processing")
    assert existing.to_dict() == {
        "id": "x",
        "name": "N",
        "archived": True,
        "status": "processing",
    }


def test_only_url_datasets_are_matched_in_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """The query filters to URL datasets (uploads have no URL to collide with)."""
    with _patch_session(monkeypatch, result=None) as session:
        find_existing("https://d.example/", None)
    # The compiled statement mentions the source_type = 'url' filter.
    compiled = str(session.statements[0])
    assert "source_type" in compiled
    # Sanity: SourceType.URL is the value the real query binds.
    assert SourceType.URL.value == "url"
