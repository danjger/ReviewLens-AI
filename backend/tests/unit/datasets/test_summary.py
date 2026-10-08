"""Unit tests for app.datasets.summary (ingestion-summary task 1.1).

These cover the snapshot pre-signed URL endpoint's domain logic offline, with
the database (``session_scope``) and S3 (``s3.presign_get``) faked at the module
boundary — the same approach the refresh-api unit tests use. The behaviour
against a real PostgreSQL + LocalStack with seeded fixtures is the integration
suite (ingestion-summary task 1.3).

What they prove (Requirement 3.1):

- :func:`snapshot_version` applies the rule: the active version, or version 1
  while the first version is still processing (no active version yet).
- :func:`snapshot_url` returns ``{url, expires_at, version}`` for a URL dataset,
  signing the key for the chosen version via ``storage.keys`` (never a hand-built
  key) with a 5-minute lifetime.
- an **upload** dataset gets a ``404`` (it has no page snapshot).
- an unknown id gets a ``404``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from app.core.errors import NotFoundError
from app.datasets import summary
from app.db.models import Dataset, DatasetStatus, SourceType
from app.storage import keys

_DS = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# snapshot_version: the pure selection rule (Requirement 3.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("active_version", "expected"),
    [
        (None, 1),  # first version still processing -> fall back to v1
        (1, 1),  # active v1
        (2, 2),  # active later version
        (7, 7),
    ],
)
def test_snapshot_version_picks_active_or_v1(active_version: int | None, expected: int) -> None:
    """Active version when set; version 1 while the first is still processing.

    _Validates: Requirement 3.1._
    """
    assert summary.snapshot_version(active_version) == expected


# ---------------------------------------------------------------------------
# Fakes / helpers for snapshot_url
# ---------------------------------------------------------------------------


def _dataset(
    *,
    source_type: SourceType = SourceType.URL,
    active_version: int | None = 2,
    status: DatasetStatus = DatasetStatus.UPDATED,
) -> Dataset:
    """A plain in-memory ``Dataset`` ORM instance (no DB session)."""
    dataset = Dataset()
    dataset.id = _DS
    dataset.name = "Example product"
    dataset.source_type = source_type
    dataset.original_url = "https://example.com/product" if source_type == SourceType.URL else None
    dataset.status = status
    dataset.active_version = active_version
    dataset.data_version = active_version or 1
    return dataset


class _FakeSession:
    """A minimal stand-in for the SQLAlchemy session: only ``get`` is used."""

    def __init__(self, dataset: Dataset | None) -> None:
        self._dataset = dataset

    def get(self, model: type[Any], ident: str) -> Dataset | None:
        return self._dataset


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake ``session_scope`` (serve one dataset) and ``presign_get``.

    Returns a dict the test tweaks: ``dataset`` is what the fake session serves,
    and ``signed`` records every ``(key, expires_in)`` the code asked S3 to sign.
    """
    state: dict[str, Any] = {"dataset": _dataset(), "signed": []}

    @contextmanager
    def _session_scope() -> Iterator[_FakeSession]:
        yield _FakeSession(state["dataset"])

    def _presign_get(key: str, *, expires_in: int) -> str:
        state["signed"].append((key, expires_in))
        return f"https://s3.example/{key}?sig=abc&expires={expires_in}"

    monkeypatch.setattr(summary, "session_scope", _session_scope)
    monkeypatch.setattr(summary.s3, "presign_get", _presign_get)
    return state


# ---------------------------------------------------------------------------
# snapshot_url: URL datasets
# ---------------------------------------------------------------------------


def test_snapshot_url_signs_active_version_key_for_url_dataset(wiring: dict[str, Any]) -> None:
    """A URL dataset returns a signed URL for the active version's snapshot key.

    _Validates: Requirement 3.1._
    """
    wiring["dataset"] = _dataset(active_version=2)

    result = summary.snapshot_url(_DS)

    assert result.version == 2
    # The signed key comes from storage.keys, never built by hand.
    assert wiring["signed"] == [(keys.dataset_snapshot(_DS, 2), summary.SNAPSHOT_URL_TTL_SECONDS)]
    assert result.url.startswith("https://s3.example/")
    assert keys.dataset_snapshot(_DS, 2) in result.url


def test_snapshot_url_falls_back_to_v1_while_processing(wiring: dict[str, Any]) -> None:
    """With no active version yet, the first version's snapshot (v1) is signed.

    _Validates: Requirement 3.1._
    """
    wiring["dataset"] = _dataset(active_version=None, status=DatasetStatus.PROCESSING)

    result = summary.snapshot_url(_DS)

    assert result.version == 1
    assert wiring["signed"] == [(keys.dataset_snapshot(_DS, 1), summary.SNAPSHOT_URL_TTL_SECONDS)]


def test_snapshot_url_expires_in_five_minutes(wiring: dict[str, Any]) -> None:
    """The URL lifetime is 5 minutes, reflected in both the sign call and expires_at.

    _Validates: Requirement 3.1 ("valid for 5 minutes")._
    """
    assert summary.SNAPSHOT_URL_TTL_SECONDS == 300

    before = datetime.now(UTC)
    result = summary.snapshot_url(_DS)

    # The sign call used the 5-minute TTL.
    assert wiring["signed"][0][1] == 300
    # expires_at is ~5 minutes in the future (allow a small execution window).
    expires_at = datetime.fromisoformat(result.expires_at)
    delta = (expires_at - before).total_seconds()
    assert 299 <= delta <= 360


def test_snapshot_url_to_dict_shape(wiring: dict[str, Any]) -> None:
    """The response serializes to exactly ``{url, expires_at, version}``."""
    data = summary.snapshot_url(_DS).to_dict()
    assert set(data.keys()) == {"url", "expires_at", "version"}
    assert isinstance(data["url"], str)
    assert isinstance(data["expires_at"], str)
    assert data["version"] == 2


# ---------------------------------------------------------------------------
# snapshot_url: 404 cases
# ---------------------------------------------------------------------------


def test_snapshot_url_404_for_upload_dataset(wiring: dict[str, Any]) -> None:
    """An upload dataset has no page snapshot -> 404, and nothing is signed.

    _Validates: Requirement 3.1 ("404 for uploads")._
    """
    wiring["dataset"] = _dataset(source_type=SourceType.UPLOAD, active_version=1)

    with pytest.raises(NotFoundError):
        summary.snapshot_url(_DS)

    assert wiring["signed"] == []


def test_snapshot_url_404_for_unknown_id(wiring: dict[str, Any]) -> None:
    """An unknown dataset id -> 404, and nothing is signed."""
    wiring["dataset"] = None

    with pytest.raises(NotFoundError):
        summary.snapshot_url(_DS)

    assert wiring["signed"] == []


# ===========================================================================
# Reviews table: list_reviews / filter_reviews / paginate (Requirements 5.1, 5.2)
# ===========================================================================
#
# These cover the reviews endpoint's domain logic offline: the pure filter and
# pagination helpers directly, and ``list_reviews`` with ``session_scope`` and
# the S3 helpers faked at the module boundary. Behaviour against real
# PostgreSQL + LocalStack is the integration suite (task 1.3); the paging
# property test is task 9.


def _review(
    rid: str,
    *,
    text: str = "Great product",
    rating: int | None = 5,
    date: str = "2026-01-01",
    author: str = "Alice",
    sentiment: str = "positive",
) -> dict[str, Any]:
    """A review item shaped like an entry of ``reviews/v{n}.json`` (metrics stage)."""
    return {
        "id": rid,
        "text": text,
        "rating": rating,
        "date": date,
        "author": author,
        "title": None,
        "sentiment": sentiment,
        "source_page": 1,
    }


# ---------------------------------------------------------------------------
# filter_reviews: each filter and combinations (Requirement 5.2)
# ---------------------------------------------------------------------------


def test_filter_reviews_no_filters_returns_all_in_order() -> None:
    """With no filters, every review is returned in its stored order.

    _Validates: Requirement 5.2._
    """
    reviews = [_review("r_0001"), _review("r_0002"), _review("r_0003")]
    assert summary.filter_reviews(reviews) == reviews


def test_filter_reviews_by_rating() -> None:
    """Rating filters to the exact star value.

    _Validates: Requirement 5.2._
    """
    reviews = [
        _review("r_0001", rating=5),
        _review("r_0002", rating=3),
        _review("r_0003", rating=5),
    ]
    result = summary.filter_reviews(reviews, rating=5)
    assert [r["id"] for r in result] == ["r_0001", "r_0003"]


def test_filter_reviews_by_sentiment_is_case_insensitive() -> None:
    """Sentiment filters by label, case-insensitively.

    _Validates: Requirement 5.2._
    """
    reviews = [
        _review("r_0001", sentiment="positive"),
        _review("r_0002", sentiment="negative"),
        _review("r_0003", sentiment="positive"),
    ]
    result = summary.filter_reviews(reviews, sentiment="POSITIVE")
    assert [r["id"] for r in result] == ["r_0001", "r_0003"]


def test_filter_reviews_text_search_is_case_insensitive_substring() -> None:
    """Text search matches a case-insensitive substring of the review text.

    _Validates: Requirement 5.2._
    """
    reviews = [
        _review("r_0001", text="Battery life is amazing"),
        _review("r_0002", text="Screen is dim"),
        _review("r_0003", text="AMAZING camera"),
    ]
    result = summary.filter_reviews(reviews, query="amazing")
    assert [r["id"] for r in result] == ["r_0001", "r_0003"]


def test_filter_reviews_combination_is_conjunctive() -> None:
    """Multiple filters combine with AND.

    _Validates: Requirement 5.2._
    """
    reviews = [
        _review("r_0001", rating=5, sentiment="positive", text="love the battery"),
        _review("r_0002", rating=5, sentiment="negative", text="battery is bad"),
        _review("r_0003", rating=5, sentiment="positive", text="great screen"),
        _review("r_0004", rating=3, sentiment="positive", text="ok battery"),
    ]
    result = summary.filter_reviews(reviews, rating=5, sentiment="positive", query="battery")
    assert [r["id"] for r in result] == ["r_0001"]


def test_filter_reviews_no_match_returns_empty() -> None:
    """A filter that matches nothing returns an empty list."""
    reviews = [_review("r_0001", rating=5)]
    assert summary.filter_reviews(reviews, rating=1) == []


# ---------------------------------------------------------------------------
# paginate: the page boundary (Requirement 5.1)
# ---------------------------------------------------------------------------


def test_paginate_first_page() -> None:
    """Page 1 returns the first ``page_size`` items."""
    items = [_review(f"r_{i:04d}") for i in range(1, 11)]
    page = summary.paginate(items, page=1, page_size=4)
    assert [r["id"] for r in page] == ["r_0001", "r_0002", "r_0003", "r_0004"]


def test_paginate_last_partial_page() -> None:
    """The final page returns the remaining items only (page boundary).

    _Validates: Requirement 5.1._
    """
    items = [_review(f"r_{i:04d}") for i in range(1, 11)]  # 10 items
    page = summary.paginate(items, page=3, page_size=4)  # items 9, 10
    assert [r["id"] for r in page] == ["r_0009", "r_0010"]


def test_paginate_past_end_is_empty() -> None:
    """A page past the end returns an empty list, not an error (page boundary)."""
    items = [_review(f"r_{i:04d}") for i in range(1, 6)]  # 5 items
    assert summary.paginate(items, page=3, page_size=4) == []


def test_paginate_concatenation_covers_every_item_once() -> None:
    """Concatenating every page reproduces the list with no gaps or dups.

    This mirrors Correctness Property 1 at the unit level (the property test is
    task 9). _Validates: Requirements 5.1, 5.2._
    """
    items = [_review(f"r_{i:04d}") for i in range(1, 24)]  # 23 items
    page_size = 5
    collected: list[dict[str, Any]] = []
    page = 1
    while True:
        chunk = summary.paginate(items, page=page, page_size=page_size)
        if not chunk:
            break
        collected.extend(chunk)
        page += 1
    assert collected == items


# ---------------------------------------------------------------------------
# list_reviews: wiring, cache, and DATA_MISSING (Requirements 5.1, 5.2)
# ---------------------------------------------------------------------------


@pytest.fixture
def reviews_wiring(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake ``session_scope`` plus the S3 helpers used by ``list_reviews``.

    ``state`` is tweakable per test: ``dataset`` is served by the fake session;
    ``objects`` maps an S3 key to a JSON document string (absence => missing);
    ``get_text_calls`` records every key actually read from S3 so a test can
    assert the in-memory cache avoided a re-read.
    """
    state: dict[str, Any] = {
        "dataset": _dataset(active_version=1, status=DatasetStatus.UPDATED),
        "objects": {},
        "get_text_calls": [],
    }

    @contextmanager
    def _session_scope() -> Iterator[_FakeSession]:
        yield _FakeSession(state["dataset"])

    def _object_exists(key: str) -> bool:
        return key in state["objects"]

    def _get_text(key: str, *, encoding: str = "utf-8") -> str:
        state["get_text_calls"].append(key)
        return str(state["objects"][key])

    monkeypatch.setattr(summary, "session_scope", _session_scope)
    monkeypatch.setattr(summary.s3, "object_exists", _object_exists)
    monkeypatch.setattr(summary.s3, "get_text", _get_text)
    summary.reset_reviews_cache()
    yield state
    summary.reset_reviews_cache()


def _seed_reviews(state: dict[str, Any], version: int, reviews: list[dict[str, Any]]) -> None:
    """Place a ``reviews/v{n}.json`` document into the fake S3 for the dataset."""
    import json as _json

    key = keys.dataset_reviews(_DS, version)
    state["objects"][key] = _json.dumps({"dataset_id": _DS, "version": version, "reviews": reviews})


def test_list_reviews_filters_then_paginates(reviews_wiring: dict[str, Any]) -> None:
    """The endpoint filters by rating/sentiment/text then paginates, with total.

    _Validates: Requirements 5.1, 5.2._
    """
    reviews = [
        _review("r_0001", rating=5, sentiment="positive", text="battery great"),
        _review("r_0002", rating=5, sentiment="positive", text="battery good"),
        _review("r_0003", rating=5, sentiment="positive", text="battery fine"),
        _review("r_0004", rating=1, sentiment="negative", text="battery dead"),
    ]
    _seed_reviews(reviews_wiring, 1, reviews)

    result = summary.list_reviews(_DS, page=1, page_size=2, rating=5, query="battery")

    assert result.total == 3  # three 5-star "battery" reviews
    assert result.page == 1
    assert [r["id"] for r in result.items] == ["r_0001", "r_0002"]  # first page of two

    page2 = summary.list_reviews(_DS, page=2, page_size=2, rating=5, query="battery")
    assert [r["id"] for r in page2.items] == ["r_0003"]


def test_list_reviews_item_exposes_required_fields(reviews_wiring: dict[str, Any]) -> None:
    """Each item exposes text, rating, date, author, and sentiment.

    _Validates: Requirement 5.1._
    """
    _seed_reviews(reviews_wiring, 1, [_review("r_0001")])

    result = summary.list_reviews(_DS)

    item = result.items[0]
    for field in ("text", "rating", "date", "author", "sentiment"):
        assert field in item


def test_list_reviews_reads_active_version_key(reviews_wiring: dict[str, Any]) -> None:
    """The active version's reviews key is the one read from S3.

    _Validates: Requirement 5.1._
    """
    reviews_wiring["dataset"] = _dataset(active_version=3, status=DatasetStatus.UPDATED)
    _seed_reviews(reviews_wiring, 3, [_review("r_0001")])

    summary.list_reviews(_DS)

    assert reviews_wiring["get_text_calls"] == [keys.dataset_reviews(_DS, 3)]


def test_list_reviews_caches_by_version(reviews_wiring: dict[str, Any]) -> None:
    """A second request for the same (id, version) does not re-read S3.

    Design "API endpoints": the parsed file is kept in memory by (id, version).
    """
    _seed_reviews(reviews_wiring, 1, [_review("r_0001"), _review("r_0002")])

    summary.list_reviews(_DS, page=1)
    summary.list_reviews(_DS, page=1, rating=5)  # different filter, same version

    # Only one S3 read happened despite two calls.
    assert reviews_wiring["get_text_calls"] == [keys.dataset_reviews(_DS, 1)]


def test_list_reviews_no_active_version_returns_empty_without_s3(
    reviews_wiring: dict[str, Any],
) -> None:
    """With no active version yet, an empty page is returned without touching S3."""
    reviews_wiring["dataset"] = _dataset(active_version=None, status=DatasetStatus.PROCESSING)

    result = summary.list_reviews(_DS)

    assert result.total == 0
    assert result.items == []
    assert reviews_wiring["get_text_calls"] == []


def test_list_reviews_404_for_unknown_id(reviews_wiring: dict[str, Any]) -> None:
    """An unknown dataset id -> 404."""
    reviews_wiring["dataset"] = None

    with pytest.raises(NotFoundError):
        summary.list_reviews(_DS)


def test_list_reviews_data_missing_when_updated_file_absent(
    reviews_wiring: dict[str, Any],
) -> None:
    """A missing active-version file while status is ``updated`` -> DATA_MISSING 500.

    _Validates: Requirement 5.1 (design "Error Handling")._
    """
    reviews_wiring["dataset"] = _dataset(active_version=2, status=DatasetStatus.UPDATED)
    # No object seeded for v2 -> object_exists is False.

    with pytest.raises(summary.DataMissingError) as excinfo:
        summary.list_reviews(_DS)

    assert excinfo.value.code == "DATA_MISSING"
    assert excinfo.value.status_code == 500
