"""Integration tests for the archive/restore API (dataset-library task 2).

These drive :mod:`app.datasets.library`'s ``archive_dataset`` /
``restore_dataset`` and the ``POST /api/datasets/{id}/archive`` /
``/restore`` endpoints against the **real** Compose PostgreSQL and LocalStack
S3, proving the design's "Integration tests" bullet and Correctness Property 2
(Requirements 5.1, 5.3, 5.4, 5.5):

- archive sets ``archived_at`` and restore clears it, and **only** ``archived_at``
  changes — every other dataset field is left byte-for-byte identical.
- no ``datasets`` row, no ``dataset_versions`` row, and no S3 object is deleted
  by either operation (Requirements 5.3, 5.4; Correctness Property 2).
- list visibility changes: an archived dataset drops out of the default
  ``archived=false`` list and appears under ``archived=true``, and restoring
  reverses that (Requirements 5.1, 5.2, 5.3).
- archiving an already-archived dataset (and restoring an active one) is a
  tolerant no-op (idempotent-handler rule).
- the detail record exposes ``archived_at`` so the chat can go read-only later
  (Requirement 5.5; the chat behaviour itself lives in ``guardrailed-chat``).

Running
-------
Run with ``make test-int`` under ``make up`` (needs the Compose PostgreSQL and
LocalStack S3). The module skips cleanly when the stack is unreachable. Each
test creates its own random dataset ids plus a unique S3 prefix and cleans up
both its rows and its S3 objects at teardown.
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

    Mirrors ``test_library_int``'s ``client`` fixture: clear
    ``ORIGIN_VERIFY_SECRET`` and reset the memoised settings so
    ``OriginGuardMiddleware`` runs in local-dev bypass mode for the headerless
    TestClient, restoring the cache on teardown.
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

    Mirrors the ``test_library_int`` factory: inserts one dataset with the given
    fields and seeds ``dataset_versions`` rows (one per version up to
    ``data_version``), completing the active version. All ids are deleted at
    teardown.
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
                    status_detail={"events": [{"status": status.value, "message": "seed"}]},
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


def _row_dict(dataset_id: str) -> dict[str, object]:
    """Read the raw ``datasets`` row as a dict (every column), for comparison."""
    with core_db.session_scope() as session:
        result = session.execute(
            text("SELECT * FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": dataset_id}
        )
        row = result.mappings().one()
        return dict(row)


# ---------------------------------------------------------------------------
# archive/restore change only archived_at (Correctness Property 2)
# ---------------------------------------------------------------------------


def test_archive_sets_only_archived_at(dataset_factory: Callable[..., str]) -> None:
    ds_id = dataset_factory(name="ToArchive", original_url="https://g2.com/a/reviews")
    before = _row_dict(ds_id)
    assert before["archived_at"] is None

    library.archive_dataset(ds_id)

    after = _row_dict(ds_id)
    assert after["archived_at"] is not None
    # Every other column is byte-for-byte identical.
    for column in before:
        if column == "archived_at":
            continue
        assert after[column] == before[column], f"column {column} changed on archive"


def test_restore_clears_only_archived_at(dataset_factory: Callable[..., str]) -> None:
    ds_id = dataset_factory(name="ToRestore", archived=True)
    before = _row_dict(ds_id)
    assert before["archived_at"] is not None

    library.restore_dataset(ds_id)

    after = _row_dict(ds_id)
    assert after["archived_at"] is None
    for column in before:
        if column == "archived_at":
            continue
        assert after[column] == before[column], f"column {column} changed on restore"


def test_archive_then_restore_round_trips_every_field(
    dataset_factory: Callable[..., str],
) -> None:
    """Property 2: archive then restore leaves every field but archived_at as-is."""
    ds_id = dataset_factory(
        name="RoundTrip",
        original_url="https://g2.com/rt/reviews",
        data_version=2,
        active_version=2,
        metrics={"review_count": 42},
    )
    before = _row_dict(ds_id)

    library.archive_dataset(ds_id)
    library.restore_dataset(ds_id)

    after = _row_dict(ds_id)
    assert after == before


# ---------------------------------------------------------------------------
# nothing is deleted (Requirements 5.3, 5.4)
# ---------------------------------------------------------------------------


def test_archive_restore_deletes_no_rows(dataset_factory: Callable[..., str]) -> None:
    ds_id = dataset_factory(name="KeepRows", data_version=3, active_version=3)

    def _counts() -> tuple[int, int]:
        with core_db.session_scope() as session:
            ds = session.execute(
                text("SELECT count(*) FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
            ).scalar_one()
            vs = session.execute(
                text("SELECT count(*) FROM dataset_versions WHERE dataset_id = CAST(:id AS uuid)"),
                {"id": ds_id},
            ).scalar_one()
            return int(ds), int(vs)

    assert _counts() == (1, 3)
    library.archive_dataset(ds_id)
    assert _counts() == (1, 3)
    library.restore_dataset(ds_id)
    assert _counts() == (1, 3)


def test_archive_restore_deletes_no_s3_objects(dataset_factory: Callable[..., str]) -> None:
    """Archiving/restoring never touches S3 objects (Requirement 5.4)."""
    from app.storage import s3 as s3_mod

    s3_mod.reset_client()
    s3 = s3_mod._get_s3_client()
    bucket = get_settings().s3_bucket
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:  # noqa: BLE001
        try:
            s3.create_bucket(Bucket=bucket)
        except Exception:  # noqa: BLE001
            pytest.skip("S3 (LocalStack) not reachable")

    ds_id = dataset_factory(name="KeepObjects")
    prefix = f"test/archive/{ds_id}/"
    keys = [f"{prefix}reviews.json", f"{prefix}v1/capture.html"]
    for key in keys:
        s3.put_object(Bucket=bucket, Key=key, Body=b"kept")

    def _listed_keys() -> set[str]:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        return {obj["Key"] for obj in resp.get("Contents", [])}

    try:
        assert _listed_keys() == set(keys)
        library.archive_dataset(ds_id)
        assert _listed_keys() == set(keys)
        library.restore_dataset(ds_id)
        assert _listed_keys() == set(keys)
    finally:
        for key in keys:
            s3.delete_object(Bucket=bucket, Key=key)


# ---------------------------------------------------------------------------
# list visibility changes (Requirements 5.1, 5.2, 5.3)
# ---------------------------------------------------------------------------


def test_archive_removes_from_default_list_and_shows_under_archived(
    dataset_factory: Callable[..., str],
) -> None:
    ds_id = dataset_factory(name="Visible")

    assert ds_id in _ids(library.list_datasets())
    assert ds_id not in _ids(library.list_datasets(archived=True))

    library.archive_dataset(ds_id)

    assert ds_id not in _ids(library.list_datasets())
    assert ds_id in _ids(library.list_datasets(archived=True))

    library.restore_dataset(ds_id)

    assert ds_id in _ids(library.list_datasets())
    assert ds_id not in _ids(library.list_datasets(archived=True))


# ---------------------------------------------------------------------------
# idempotency (idempotent-handler rule)
# ---------------------------------------------------------------------------


def test_archive_twice_is_noop_and_keeps_timestamp(
    dataset_factory: Callable[..., str],
) -> None:
    ds_id = dataset_factory(name="Idempotent")

    first = library.archive_dataset(ds_id).to_dict()
    first_archived_at = first["archived_at"]
    assert first_archived_at is not None

    second = library.archive_dataset(ds_id).to_dict()
    # The original timestamp is preserved rather than overwritten.
    assert second["archived_at"] == first_archived_at


def test_restore_active_dataset_is_noop(dataset_factory: Callable[..., str]) -> None:
    ds_id = dataset_factory(name="AlreadyActive")
    assert _row_dict(ds_id)["archived_at"] is None

    detail = library.restore_dataset(ds_id).to_dict()
    assert detail["archived_at"] is None


# ---------------------------------------------------------------------------
# detail record exposes archived_at (Requirement 5.5)
# ---------------------------------------------------------------------------


def test_detail_exposes_archived_at(dataset_factory: Callable[..., str]) -> None:
    ds_id = dataset_factory(name="DetailArchived")
    detail = library.get_dataset(ds_id).to_dict()
    assert "archived_at" in detail
    assert detail["archived_at"] is None

    library.archive_dataset(ds_id)
    detail = library.get_dataset(ds_id).to_dict()
    assert detail["archived_at"] is not None


# ---------------------------------------------------------------------------
# HTTP layer (endpoints wired under /api)
# ---------------------------------------------------------------------------


def test_http_archive_and_restore(client: TestClient, dataset_factory: Callable[..., str]) -> None:
    ds_id = dataset_factory(name="HTTP archive")

    # Archive -> archived_at set, drops from default list.
    resp = client.post(f"/api/datasets/{ds_id}/archive")
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is not None

    resp = client.get("/api/datasets")
    assert ds_id not in [row["id"] for row in resp.json()["datasets"]]
    resp = client.get("/api/datasets?archived=true")
    assert ds_id in [row["id"] for row in resp.json()["datasets"]]

    # Restore -> archived_at cleared, back in default list.
    resp = client.post(f"/api/datasets/{ds_id}/restore")
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is None

    resp = client.get("/api/datasets")
    assert ds_id in [row["id"] for row in resp.json()["datasets"]]


def test_http_archive_unknown_id_is_404(client: TestClient) -> None:
    resp = client.post(f"/api/datasets/{uuid.uuid4()}/archive")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_http_restore_unknown_id_is_404(client: TestClient) -> None:
    resp = client.post(f"/api/datasets/{uuid.uuid4()}/restore")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
