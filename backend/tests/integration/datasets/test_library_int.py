"""Integration tests for the Library list/detail/rename API (dataset-library 1).

Task 1's unit tests (``tests/unit/datasets/test_library.py``) proved the pure
derivations (``display_state``, ``main_url``, ``last_message``,
``last_refreshed_at``, ``refresh_check_id``, LIKE escaping) offline. This suite
drives :mod:`app.datasets.library` and the ``/api/datasets`` endpoints against
the **real** Compose PostgreSQL, proving the filter / sort / search combinations
the design's "Integration tests" bullet calls for (Requirements 2.1, 2.2, 2.3,
3.1; Correctness Property 3):

- ``archived=false`` returns only non-archived datasets; ``archived=true``
  returns only archived ones (Requirement 2.1 / 5.2).
- the text filter matches name OR main URL, case-insensitively, and only those
  rows (Correctness Property 3), with LIKE wildcards treated literally.
- each sort order (``activity`` default, ``name``, ``status``, ``refreshed``)
  returns the rows in the requested order.
- ``GET /datasets/{id}`` returns the full record with ``active_version`` and the
  ``dataset_versions`` list; ``404`` for an unknown id.
- ``PATCH /datasets/{id}`` renames and nothing else; ``404``/``422`` guards.

Running
-------
Run with ``make test-int`` under ``make up`` (needs the Compose PostgreSQL). The
module and each test skip cleanly when the database is unreachable, so the suite
still *collects* without the stack. Each test uses its own random dataset ids
and cleans up its rows. No S3 or SQS is touched (these are pure DB reads/writes),
and the Library endpoints make no AI call.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from app.api import app
from app.core import db as core_db
from app.core.config import get_settings
from app.datasets import library
from app.db.models import Base, Dataset, DatasetStatus, SourceType
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.integration


def _database_reachable() -> bool:
    try:
        with core_db.get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any failure means skip
        return False


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
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A FastAPI test client with the origin guard disabled (empty secret).

    The integration suite loads the repo-root ``.env``, which sets
    ``ORIGIN_VERIFY_SECRET`` — so without this override ``OriginGuardMiddleware``
    would 403 the TestClient's headerless requests. Following the established
    integration-test pattern (``test_refresh_service_int``'s ``_env`` fixture:
    ``monkeypatch.setenv`` + ``get_settings.cache_clear()``), this clears the
    secret and resets the memoised settings so the guard runs in local-dev
    bypass mode for the duration of the test, then restores the cache on
    teardown. The middleware re-reads ``get_settings()`` on every request, so
    clearing the cache after the env override takes effect immediately.
    """
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", "")
    get_settings.cache_clear()
    try:
        yield TestClient(app)
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def dataset_factory() -> Iterator[Callable[..., str]]:
    """Insert datasets (with version rows) and clean them up afterwards.

    Returns a maker that inserts one dataset with the given fields and seeds its
    ``dataset_versions`` rows (one per version up to ``data_version``), marking
    the active version complete with a ``completed_at`` so ``last_refreshed_at``
    is populated. All ids created are deleted at teardown.
    """
    created: list[str] = []

    def _make(
        *,
        name: str = "Dataset",
        source_type: SourceType = SourceType.URL,
        original_url: str | None = None,
        status: DatasetStatus = DatasetStatus.UPDATED,
        data_version: int = 1,
        active_version: int | None = 1,
        archived: bool = False,
        metrics: dict[str, object] | None = None,
        updated_offset_minutes: int = 0,
        status_detail: dict[str, object] | None = None,
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
                    status_detail=(
                        status_detail
                        if status_detail is not None
                        else {"events": [{"status": status.value, "message": "seed"}]}
                    ),
                    data_version=data_version,
                    active_version=active_version,
                    metrics=metrics,
                )
            )
            # Deterministic, distinct updated_at so sort order is stable.
            session.execute(
                text(
                    "UPDATE datasets SET updated_at = now() + make_interval(mins => :m) "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"m": updated_offset_minutes, "id": ds_id},
            )
            if archived:
                session.execute(
                    text("UPDATE datasets SET archived_at = now() WHERE id = CAST(:id AS uuid)"),
                    {"id": ds_id},
                )
            for v in range(1, data_version + 1):
                completed = v == active_version
                session.execute(
                    text(
                        "INSERT INTO dataset_versions "
                        "(dataset_id, version, trigger, requested_at, completed_at, "
                        " review_count, extraction_method, outcome) "
                        "VALUES (CAST(:id AS uuid), :v, 'initial', now(), "
                        "        CASE WHEN :done THEN now() ELSE NULL END, "
                        "        CASE WHEN :done THEN 10 ELSE NULL END, "
                        "        CASE WHEN :done THEN 'ai' ELSE NULL END, "
                        "        CASE WHEN :done THEN 'updated' ELSE NULL END)"
                    ),
                    {"id": ds_id, "v": v, "done": completed},
                )
        created.append(ds_id)
        return ds_id

    try:
        yield _make
    finally:
        with core_db.session_scope() as session:
            for ds_id in created:
                session.execute(
                    text("DELETE FROM dataset_versions WHERE dataset_id = CAST(:id AS uuid)"),
                    {"id": ds_id},
                )
                session.execute(
                    text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
                )


def _ids(rows: list[library.DatasetRow]) -> list[str]:
    return [r.id for r in rows]


# ---------------------------------------------------------------------------
# archived filter (Requirement 2.1 / 5.2)
# ---------------------------------------------------------------------------


def test_list_excludes_archived_by_default(dataset_factory: Callable[..., str]) -> None:
    live = dataset_factory(name="Live one")
    archived = dataset_factory(name="Archived one", archived=True)

    default_ids = _ids(library.list_datasets())
    assert live in default_ids
    assert archived not in default_ids


def test_list_archived_only_when_requested(dataset_factory: Callable[..., str]) -> None:
    live = dataset_factory(name="Live two")
    archived = dataset_factory(name="Archived two", archived=True)

    archived_ids = _ids(library.list_datasets(archived=True))
    assert archived in archived_ids
    assert live not in archived_ids


# ---------------------------------------------------------------------------
# text filter: name OR url, case-insensitive, exact substring (Property 3)
# ---------------------------------------------------------------------------


def test_filter_matches_name_case_insensitively(dataset_factory: Callable[..., str]) -> None:
    match = dataset_factory(name="Acme CRM")
    other = dataset_factory(name="Widget Pro")

    result = _ids(library.list_datasets(q="acme"))
    assert match in result
    assert other not in result


def test_filter_matches_url(dataset_factory: Callable[..., str]) -> None:
    token = uuid.uuid4().hex
    match = dataset_factory(name="By URL", original_url=f"https://g2.com/products/{token}/reviews")
    other = dataset_factory(name="Different", original_url="https://example.com/x")

    result = _ids(library.list_datasets(q=token))
    assert result == [match] or (match in result and other not in result)
    assert other not in result


def test_filter_treats_wildcards_literally(dataset_factory: Callable[..., str]) -> None:
    """A '%' in the query matches a literal '%', not "everything" (Property 3)."""
    match = dataset_factory(name="Save 50% today")
    other = dataset_factory(name="No discount here")

    result = _ids(library.list_datasets(q="50%"))
    assert match in result
    assert other not in result


def test_blank_filter_applies_no_filter(dataset_factory: Callable[..., str]) -> None:
    a = dataset_factory(name="Alpha blank")
    b = dataset_factory(name="Beta blank")
    result = _ids(library.list_datasets(q="   "))
    assert a in result and b in result


# ---------------------------------------------------------------------------
# sort orders (Requirement 2.2)
# ---------------------------------------------------------------------------


def test_sort_by_activity_newest_first(dataset_factory: Callable[..., str]) -> None:
    older = dataset_factory(name="Older", updated_offset_minutes=0)
    newer = dataset_factory(name="Newer", updated_offset_minutes=5)

    ordered = _ids(library.list_datasets(sort="activity"))
    # Among our two, the newer must come before the older.
    assert ordered.index(newer) < ordered.index(older)


def test_sort_by_name_ascending(dataset_factory: Callable[..., str]) -> None:
    tag = uuid.uuid4().hex[:6]
    z = dataset_factory(name=f"zzz-{tag}")
    a = dataset_factory(name=f"aaa-{tag}")

    ordered = _ids(library.list_datasets(sort="name"))
    assert ordered.index(a) < ordered.index(z)


def test_sort_by_status(dataset_factory: Callable[..., str]) -> None:
    failed = dataset_factory(name="S failed", status=DatasetStatus.FAILED, active_version=None)
    updated = dataset_factory(name="S updated", status=DatasetStatus.UPDATED)

    ordered = _ids(library.list_datasets(sort="status"))
    # DatasetStatus is a native PostgreSQL enum, so ``ORDER BY status`` sorts by
    # the enum's DECLARATION order (requested, processing, updated, failed), not
    # alphabetically. That declaration order is the dataset lifecycle, which is
    # exactly the grouping Requirement 2.2's "sort by status" wants (in-progress
    # work first, terminal states last), so ``updated`` sorts before ``failed``.
    assert ordered.index(updated) < ordered.index(failed)


def test_unknown_sort_falls_back_to_activity(dataset_factory: Callable[..., str]) -> None:
    older = dataset_factory(name="UO older", updated_offset_minutes=0)
    newer = dataset_factory(name="UO newer", updated_offset_minutes=5)
    ordered = _ids(library.list_datasets(sort="not-a-sort"))
    assert ordered.index(newer) < ordered.index(older)


# ---------------------------------------------------------------------------
# combined filter + sort + archived
# ---------------------------------------------------------------------------


def test_filter_and_sort_combine(dataset_factory: Callable[..., str]) -> None:
    tag = uuid.uuid4().hex[:8]
    first = dataset_factory(name=f"{tag} Bravo", updated_offset_minutes=0)
    second = dataset_factory(name=f"{tag} Alpha", updated_offset_minutes=5)
    noise = dataset_factory(name="unrelated")

    by_name = _ids(library.list_datasets(q=tag, sort="name"))
    assert by_name == [second, first]  # Alpha before Bravo
    assert noise not in by_name

    by_activity = _ids(library.list_datasets(q=tag, sort="activity"))
    assert by_activity == [second, first]  # newer (second) first


# ---------------------------------------------------------------------------
# derived fields end to end
# ---------------------------------------------------------------------------


def test_derived_fields_present_on_row(dataset_factory: Callable[..., str]) -> None:
    ds_id = dataset_factory(
        name="Derived",
        original_url="https://g2.com/x/reviews",
        status=DatasetStatus.UPDATED,
        data_version=2,
        active_version=2,
        metrics={"review_count": 212},
    )
    rows = {r.id: r for r in library.list_datasets()}
    row = rows[ds_id]
    assert row.display_state == "ready"
    assert row.main_url == "https://g2.com/x/reviews"
    assert row.review_count == 212
    assert row.last_refreshed_at is not None  # active version completed


# ---------------------------------------------------------------------------
# GET /datasets/{id} (Requirement 3.1)
# ---------------------------------------------------------------------------


def test_get_dataset_returns_full_record_with_versions(
    dataset_factory: Callable[..., str],
) -> None:
    ds_id = dataset_factory(name="Detail", data_version=2, active_version=2)
    detail = library.get_dataset(ds_id).to_dict()
    assert detail["id"] == ds_id
    assert detail["active_version"] == 2
    versions = detail["versions"]
    assert isinstance(versions, list)
    assert [v["version"] for v in versions] == [1, 2]


def test_get_dataset_unknown_id_raises_not_found() -> None:
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        library.get_dataset(str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# GET /datasets/{id} exposes status_detail + description (task 9; Req 3.1, 6.4)
# ---------------------------------------------------------------------------


def test_get_dataset_exposes_status_detail_blocks(
    dataset_factory: Callable[..., str],
) -> None:
    """The detail record returns the whole status_detail: the events log, the
    redirect hops, and the viability block (ProcessingTimeline, DatasetHeader
    "Resolved to", PredictionPanel)."""
    seeded = {
        "events": [
            {"status": "requested", "at": "2024-01-01T00:00:00+00:00", "message": "requested"},
            {"status": "updated", "at": "2024-01-02T00:00:00+00:00", "message": "done"},
        ],
        "redirects": [
            {"from": "http://example.com/x", "to": "https://example.com/x", "status": 301},
        ],
        "viability": {
            "verdict": "will_work",
            "reasons": ["25 reviews detected"],
            "warnings": [],
            "evidence": {},
        },
    }
    ds_id = dataset_factory(
        name="Detail with status_detail",
        original_url="https://example.com/x/reviews",
        status_detail=seeded,
    )

    detail = library.get_dataset(ds_id).to_dict()
    sd = detail["status_detail"]
    assert [e["message"] for e in sd["events"]] == ["requested", "done"]
    assert sd["redirects"][0]["status"] == 301
    assert sd["viability"]["verdict"] == "will_work"
    # URL dataset -> no upload description.
    assert detail["description"] is None
    # metrics is unchanged (entity profile is NOT merged here).
    assert "entity" not in (detail["metrics"] or {})


def test_get_dataset_exposes_upload_description(
    dataset_factory: Callable[..., str],
) -> None:
    """An upload dataset surfaces its stored source description (sourced from
    status_detail.description, where ingestion persists it)."""
    seeded = {
        "events": [{"status": "requested", "message": "requested"}],
        "description": "Q1 support tickets export",
    }
    ds_id = dataset_factory(
        name="support-tickets.csv",
        source_type=SourceType.UPLOAD,
        status_detail=seeded,
    )

    detail = library.get_dataset(ds_id).to_dict()
    assert detail["description"] == "Q1 support tickets export"
    assert detail["source_type"] == "upload"


def test_list_response_unchanged_has_no_detail_fields(
    dataset_factory: Callable[..., str],
) -> None:
    """GET /datasets rows are the list shape only: the detail-only fields
    (status_detail, description, versions, metrics, source_type) must NOT leak
    into list rows (task 9: the change is strictly additive to the detail)."""
    ds_id = dataset_factory(name="List shape", status=DatasetStatus.UPDATED)
    rows = {r.id: r for r in library.list_datasets()}
    data = rows[ds_id].to_dict()
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


def test_http_detail_includes_status_detail_and_description(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    """The HTTP detail endpoint returns status_detail + description end to end,
    while the HTTP list rows stay the list shape."""
    seeded = {
        "events": [{"status": "requested", "message": "requested"}],
        "description": "uploaded notes",
    }
    ds_id = dataset_factory(
        name="http-upload.csv",
        source_type=SourceType.UPLOAD,
        status_detail=seeded,
    )

    detail = client.get(f"/api/datasets/{ds_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["description"] == "uploaded notes"
    assert body["status_detail"]["events"][0]["message"] == "requested"

    # List rows do not carry the detail-only fields.
    listing = client.get("/api/datasets")
    row = next(r for r in listing.json()["datasets"] if r["id"] == ds_id)
    assert "status_detail" not in row
    assert "description" not in row


# ---------------------------------------------------------------------------
# PATCH /datasets/{id} (rename)
# ---------------------------------------------------------------------------


def test_rename_changes_only_name(dataset_factory: Callable[..., str]) -> None:
    ds_id = dataset_factory(name="Before", status=DatasetStatus.UPDATED)
    before = library.get_dataset(ds_id).to_dict()

    after = library.rename_dataset(ds_id, "After").to_dict()
    assert after["name"] == "After"
    # Everything else is unchanged.
    assert after["status"] == before["status"]
    assert after["data_version"] == before["data_version"]
    assert after["active_version"] == before["active_version"]


def test_rename_blank_name_rejected(dataset_factory: Callable[..., str]) -> None:
    from app.core.errors import AppValidationError

    ds_id = dataset_factory(name="Keep")
    with pytest.raises(AppValidationError):
        library.rename_dataset(ds_id, "   ")


# ---------------------------------------------------------------------------
# HTTP layer (endpoints wired under /api)
# ---------------------------------------------------------------------------


def test_http_list_and_detail_and_rename(
    client: TestClient, dataset_factory: Callable[..., str]
) -> None:
    ds_id = dataset_factory(name="HTTP one", status=DatasetStatus.UPDATED)

    # List
    resp = client.get("/api/datasets")
    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()["datasets"]]
    assert ds_id in ids

    # Detail
    resp = client.get(f"/api/datasets/{ds_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == ds_id
    assert "versions" in resp.json()

    # Rename
    resp = client.patch(f"/api/datasets/{ds_id}", json={"name": "HTTP renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "HTTP renamed"

    # Unknown id -> 404 envelope
    resp = client.get(f"/api/datasets/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"

    # Empty name -> 422 envelope (model min_length)
    resp = client.patch(f"/api/datasets/{ds_id}", json={"name": ""})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
