"""Integration tests for the summary endpoints with seeded fixtures (ingestion-summary 1.3).

Tasks 1.1 and 1.2's unit tests (``tests/unit/datasets/test_summary.py``) proved
the snapshot-URL rule, the reviews filter / pagination helpers, the ``(id,
version)`` cache, and the ``DATA_MISSING`` guard offline, with ``session_scope``
and the S3 helpers faked at the module boundary. This suite is task 1.3: it
drives the three endpoints the detail page's summary section reads against the
**real** backing services the design's "Backend integration tests" bullet names
— the Compose PostgreSQL database and the LocalStack S3 bucket — with seeded
fixtures (no live AI, no real websites):

- ``GET /datasets/{id}`` — the detail endpoint. This endpoint is **owned by
  ``dataset-library``** (``app.datasets.api`` / ``app.datasets.library``); this
  suite does not implement it, it exercises it with seeded fixtures because it
  feeds the summary page. It returns the full record: metadata, ``metrics`` for
  the active version, ``status_detail``, ``data_version``, ``active_version``,
  ``display_state``, the ``versions`` list, and ``archived_at`` (Requirement 2.1).
- ``GET /datasets/{id}/reviews`` — pagination, rating / sentiment filters, and
  text search against a seeded ``reviews/v{active_version}.json`` in S3, plus the
  ``DATA_MISSING`` 500 when the active-version file is absent (Requirement 5.1).
- ``GET /datasets/{id}/snapshot-url`` — a 5-minute pre-signed URL for a URL
  dataset, ``404`` for an upload, and ``404`` for an unknown id (Requirement 3.1).

Running
-------
Run with ``make test-int`` under ``make up`` (needs LocalStack **and** the
Compose PostgreSQL). The module and each test skip cleanly when either is
unavailable, so the suite still *collects* without the stack. Each test uses its
own random dataset ids and cleans up its database rows and its ``datasets/{id}/``
S3 prefix. S3 keys come only from :mod:`app.storage.keys`; no SQS, EventBridge,
or AI is touched (these endpoints are pure DB + S3 reads).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from app.api import app
from app.core import db as core_db
from app.core.config import get_settings
from app.db.models import Base, Dataset, DatasetStatus, SourceType
from app.storage import keys, s3
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"


# ---------------------------------------------------------------------------
# Availability probes (skip cleanly without the stack)
# ---------------------------------------------------------------------------


def _localstack_up() -> bool:
    try:
        resp = httpx.get(f"{_ENDPOINT}/_localstack/health", timeout=2.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _database_reachable() -> bool:
    try:
        with core_db.get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any failure means skip
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _require_stack() -> None:
    if not _localstack_up():
        pytest.skip("LocalStack not reachable; run under `make test-int`")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point AWS clients at LocalStack, disable the origin guard, reset handles.

    The integration suite loads the repo-root ``.env`` (which sets
    ``ORIGIN_VERIFY_SECRET`` and the AWS endpoint); without the overrides here
    ``OriginGuardMiddleware`` would 403 the TestClient's headerless requests and
    the S3 client would point at real AWS. Following the established pattern
    (``test_refresh_service_int``'s ``_env`` + ``test_library_int``'s ``client``
    fixtures): set the LocalStack env, clear ``ORIGIN_VERIFY_SECRET`` so the
    guard runs in local-dev bypass mode, clear the memoised settings so the
    middleware re-reads them, and reset the cached S3 client so it rebinds to
    LocalStack. Restore the settings cache on teardown.
    """
    monkeypatch.setenv("AWS_ENDPOINT_URL", _ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", _S3_BUCKET)
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", "")
    get_settings.cache_clear()
    s3.reset_client()
    try:
        yield
    finally:
        get_settings.cache_clear()
        s3.reset_client()


@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    """Ensure the DB schema exists; skip when no PostgreSQL is reachable."""
    get_settings.cache_clear()
    core_db.reset_engine()
    if get_settings().is_aws or not _database_reachable():
        pytest.skip("PostgreSQL not reachable; run under `make test-int` with the stack up")
    engine = core_db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(engine)
    yield
    core_db.reset_engine()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """A FastAPI test client (origin guard disabled via the ``_env`` fixture)."""
    from app.datasets import summary

    summary.reset_reviews_cache()
    try:
        yield TestClient(app)
    finally:
        summary.reset_reviews_cache()


@pytest.fixture()
def dataset_factory() -> Iterator[Callable[..., str]]:
    """Insert a dataset (+ version rows), optionally seed its reviews/snapshot in S3.

    Returns a maker that inserts one dataset with the given fields, seeds its
    ``dataset_versions`` rows (one per version up to ``data_version``, with the
    active version marked complete), and — when asked — writes a
    ``reviews/v{active_version}.json`` and/or a ``snapshot/v{active_version}.png``
    to the LocalStack bucket using keys from :mod:`app.storage.keys`. Every
    database row and the whole ``datasets/{id}/`` S3 prefix are removed at
    teardown so each test owns its own ids (testing convention).
    """
    created: list[str] = []

    def _make(
        *,
        name: str = "Summary dataset",
        source_type: SourceType = SourceType.URL,
        original_url: str | None = "https://example.com/product/reviews",
        status: DatasetStatus = DatasetStatus.UPDATED,
        data_version: int = 1,
        active_version: int | None = 1,
        archived: bool = False,
        metrics: dict[str, Any] | None = None,
        reviews: list[dict[str, Any]] | None = None,
        with_snapshot: bool = False,
    ) -> str:
        ds_id = str(uuid.uuid4())
        url = original_url if source_type == SourceType.URL else None
        with core_db.session_scope() as session:
            session.add(
                Dataset(
                    id=ds_id,
                    name=name,
                    source_type=source_type,
                    original_url=url,
                    final_url=url,
                    normalized_url=url,
                    normalized_final_url=url,
                    platform="example.com" if url else None,
                    status=status,
                    status_detail={"events": [{"status": status.value, "message": "seed event"}]},
                    data_version=data_version,
                    active_version=active_version,
                    metrics=metrics,
                )
            )
            if archived:
                session.execute(
                    text("UPDATE datasets SET archived_at = now() WHERE id = CAST(:id AS uuid)"),
                    {"id": ds_id},
                )
            for v in range(1, data_version + 1):
                done = v == active_version
                session.execute(
                    text(
                        "INSERT INTO dataset_versions "
                        "(dataset_id, version, trigger, requested_at, completed_at, "
                        " review_count, extraction_method, outcome) "
                        "VALUES (CAST(:id AS uuid), :v, 'initial', now(), "
                        "        CASE WHEN :done THEN now() ELSE NULL END, "
                        "        CASE WHEN :done THEN :rc ELSE NULL END, "
                        "        CASE WHEN :done THEN 'ai' ELSE NULL END, "
                        "        CASE WHEN :done THEN 'updated' ELSE NULL END)"
                    ),
                    {
                        "id": ds_id,
                        "v": v,
                        "done": done,
                        "rc": (len(reviews) if reviews is not None else 10),
                    },
                )
        created.append(ds_id)

        if reviews is not None and active_version is not None:
            key = keys.dataset_reviews(ds_id, active_version)
            s3.put_bytes(
                key,
                json.dumps(
                    {"dataset_id": ds_id, "version": active_version, "reviews": reviews}
                ).encode("utf-8"),
                content_type="application/json",
            )
        if with_snapshot and active_version is not None:
            s3.put_bytes(
                keys.dataset_snapshot(ds_id, active_version),
                b"\x89PNG snapshot bytes",
                content_type="image/png",
            )
        return ds_id

    try:
        yield _make
    finally:
        s3_client = s3._get_s3_client()
        with core_db.session_scope() as session:
            for ds_id in created:
                session.execute(
                    text("DELETE FROM dataset_versions WHERE dataset_id = CAST(:id AS uuid)"),
                    {"id": ds_id},
                )
                session.execute(
                    text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
                )
        for ds_id in created:
            listed = s3_client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=keys.dataset_prefix(ds_id))
            for obj in listed.get("Contents", []):
                obj_key = obj.get("Key")
                if obj_key:
                    s3_client.delete_object(Bucket=_S3_BUCKET, Key=obj_key)


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


# ===========================================================================
# GET /datasets/{id} — the detail endpoint (owned by dataset-library)
# Requirement 2.1: the summary reads the full record it feeds on.
# ===========================================================================


def test_detail_returns_full_record_for_active_version(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """The detail endpoint returns the full record the summary page consumes.

    Covers the fields the design's "API endpoints" table lists for
    ``GET /datasets/{id}``: metadata, ``metrics`` (active version), ``status_detail``,
    ``data_version``, ``active_version``, ``display_state``, the ``versions`` list,
    and ``archived_at``. _Validates: Requirement 2.1._
    """
    metrics = {
        "review_count": 212,
        "average_rating": 4.3,
        "sentiment": {"positive": 180, "neutral": 20, "negative": 12},
    }
    ds_id = dataset_factory(
        name="Acme CRM",
        status=DatasetStatus.UPDATED,
        data_version=2,
        active_version=2,
        metrics=metrics,
    )

    resp = client.get(f"/api/datasets/{ds_id}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["id"] == ds_id
    assert body["name"] == "Acme CRM"
    assert body["source_type"] == "url"
    assert body["data_version"] == 2
    assert body["active_version"] == 2
    assert body["display_state"] == "ready"
    assert body["archived_at"] is None
    # Active-version metrics are carried through verbatim.
    assert body["metrics"] == metrics
    assert body["review_count"] == 212
    # status_detail's latest message is surfaced as last_message.
    assert body["last_message"] == "seed event"
    # The versions list is present and ordered.
    assert [v["version"] for v in body["versions"]] == [1, 2]
    active = next(v for v in body["versions"] if v["version"] == 2)
    assert active["outcome"] == "updated"
    assert active["completed_at"] is not None


def test_detail_reports_archived_at_for_archived_dataset(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """An archived dataset's detail carries ``archived_at`` (Requirement 2.1)."""
    ds_id = dataset_factory(name="Archived", archived=True)

    resp = client.get(f"/api/datasets/{ds_id}")
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is not None


def test_detail_404_for_unknown_id(client: TestClient) -> None:
    """An unknown id returns the 404 error envelope."""
    resp = client.get(f"/api/datasets/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ===========================================================================
# GET /datasets/{id}/reviews — pagination, filters, search (Requirement 5.1)
# against a seeded reviews/v{n}.json in S3.
# ===========================================================================


def test_reviews_paginates_seeded_file(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """Pages slice the seeded reviews file; total is the full match count.

    _Validates: Requirement 5.1._
    """
    reviews = [_review(f"r_{i:04d}") for i in range(1, 11)]  # 10 reviews
    ds_id = dataset_factory(active_version=1, reviews=reviews)

    page1 = client.get(f"/api/datasets/{ds_id}/reviews", params={"page": 1, "page_size": 4})
    assert page1.status_code == 200
    body1 = page1.json()
    assert body1["total"] == 10
    assert body1["page"] == 1
    assert [r["id"] for r in body1["items"]] == ["r_0001", "r_0002", "r_0003", "r_0004"]

    page3 = client.get(f"/api/datasets/{ds_id}/reviews", params={"page": 3, "page_size": 4})
    body3 = page3.json()
    assert [r["id"] for r in body3["items"]] == ["r_0009", "r_0010"]  # final partial page

    # A page past the end is empty, not an error, and total is unchanged.
    page4 = client.get(f"/api/datasets/{ds_id}/reviews", params={"page": 4, "page_size": 4})
    assert page4.status_code == 200
    assert page4.json()["items"] == []
    assert page4.json()["total"] == 10


def test_reviews_concatenated_pages_cover_every_match_once(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """Walking every page of a filter reproduces the matches with no gaps or dups.

    This exercises Correctness Property 1 end-to-end against the real S3-backed
    endpoint (the Hypothesis property test is task 9). _Validates: Requirement 5.1._
    """
    reviews = [_review(f"r_{i:04d}", rating=(5 if i % 2 else 3)) for i in range(1, 24)]
    ds_id = dataset_factory(active_version=1, reviews=reviews)

    collected: list[str] = []
    page = 1
    total: int | None = None
    while True:
        resp = client.get(
            f"/api/datasets/{ds_id}/reviews", params={"page": page, "page_size": 5, "rating": 5}
        )
        assert resp.status_code == 200
        body = resp.json()
        total = body["total"]
        if not body["items"]:
            break
        collected.extend(r["id"] for r in body["items"])
        page += 1

    expected = [r["id"] for r in reviews if r["rating"] == 5]
    assert collected == expected  # stable order, no gaps
    assert len(collected) == len(set(collected))  # no duplicates
    assert total == len(expected)


def test_reviews_filters_by_rating_sentiment_and_search(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """Rating, sentiment, and text search combine conjunctively over S3 data.

    _Validates: Requirement 5.1._
    """
    reviews = [
        _review("r_0001", rating=5, sentiment="positive", text="love the battery"),
        _review("r_0002", rating=5, sentiment="negative", text="battery is bad"),
        _review("r_0003", rating=5, sentiment="positive", text="great screen"),
        _review("r_0004", rating=3, sentiment="positive", text="ok battery"),
    ]
    ds_id = dataset_factory(active_version=1, reviews=reviews)

    # rating filter
    by_rating = client.get(f"/api/datasets/{ds_id}/reviews", params={"rating": 5}).json()
    assert sorted(r["id"] for r in by_rating["items"]) == ["r_0001", "r_0002", "r_0003"]

    # sentiment filter (case-insensitive)
    by_sentiment = client.get(
        f"/api/datasets/{ds_id}/reviews", params={"sentiment": "POSITIVE"}
    ).json()
    assert sorted(r["id"] for r in by_sentiment["items"]) == ["r_0001", "r_0003", "r_0004"]

    # text search (case-insensitive substring)
    by_text = client.get(f"/api/datasets/{ds_id}/reviews", params={"q": "battery"}).json()
    assert sorted(r["id"] for r in by_text["items"]) == ["r_0001", "r_0002", "r_0004"]

    # combination is AND
    combo = client.get(
        f"/api/datasets/{ds_id}/reviews",
        params={"rating": 5, "sentiment": "positive", "q": "battery"},
    ).json()
    assert [r["id"] for r in combo["items"]] == ["r_0001"]
    assert combo["total"] == 1


def test_reviews_items_expose_required_fields(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """Each item exposes text, rating, date, author, and sentiment (Requirement 5.1)."""
    ds_id = dataset_factory(active_version=1, reviews=[_review("r_0001")])

    item = client.get(f"/api/datasets/{ds_id}/reviews").json()["items"][0]
    for field in ("text", "rating", "date", "author", "sentiment"):
        assert field in item


def test_reviews_no_active_version_returns_empty(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """A still-processing first version (no active version) returns an empty page."""
    ds_id = dataset_factory(
        status=DatasetStatus.PROCESSING, data_version=1, active_version=None, reviews=None
    )

    body = client.get(f"/api/datasets/{ds_id}/reviews").json()
    assert body["total"] == 0
    assert body["items"] == []


def test_reviews_data_missing_when_updated_file_absent(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """An ``updated`` dataset whose active-version file is absent -> 500 DATA_MISSING.

    The dataset is seeded ``updated`` with ``active_version=1`` but no reviews
    file is written to S3, so the endpoint hits the design's "Error Handling"
    invariant breach. _Validates: Requirement 5.1._
    """
    ds_id = dataset_factory(
        status=DatasetStatus.UPDATED, data_version=1, active_version=1, reviews=None
    )

    resp = client.get(f"/api/datasets/{ds_id}/reviews")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "DATA_MISSING"


def test_reviews_404_for_unknown_id(client: TestClient) -> None:
    """An unknown dataset id -> 404 envelope."""
    resp = client.get(f"/api/datasets/{uuid.uuid4()}/reviews")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ===========================================================================
# GET /datasets/{id}/snapshot-url — pre-signed URL (Requirement 3.1)
# ===========================================================================


def test_snapshot_url_returns_presigned_url_for_url_dataset(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """A URL dataset returns ``{url, expires_at, version}`` for the active version.

    The signed URL points at the active version's snapshot key and actually
    resolves the seeded object from LocalStack when followed, confirming it is a
    real, usable pre-signed GET. _Validates: Requirement 3.1._
    """
    ds_id = dataset_factory(active_version=2, data_version=2, with_snapshot=True)

    resp = client.get(f"/api/datasets/{ds_id}/snapshot-url")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"url", "expires_at", "version"}
    assert body["version"] == 2
    assert isinstance(body["url"], str) and body["url"].startswith("http")

    # The pre-signed GET actually resolves the seeded object through LocalStack.
    fetched = httpx.get(body["url"], timeout=5.0)
    assert fetched.status_code == 200
    assert fetched.content == b"\x89PNG snapshot bytes"


def test_snapshot_url_404_for_upload_dataset(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """An upload dataset has no page snapshot -> 404 (Requirement 3.1)."""
    ds_id = dataset_factory(
        name="uploaded.csv", source_type=SourceType.UPLOAD, original_url=None, active_version=1
    )

    resp = client.get(f"/api/datasets/{ds_id}/snapshot-url")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_snapshot_url_404_for_unknown_id(client: TestClient) -> None:
    """An unknown dataset id -> 404 (Requirement 3.1)."""
    resp = client.get(f"/api/datasets/{uuid.uuid4()}/snapshot-url")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
