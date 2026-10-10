"""Integration test for the ``html_upload`` ``source_type`` migration.

dataset-ingestion task 15 (Requirements 8.8, 8.11). Runs the real Alembic
migration chain (``0001`` → ``0002_html_upload_source_type``) against the live
Compose PostgreSQL and proves the two behaviours the task requires:

1. the new native enum value ``html_upload`` is **accepted on insert** into
   ``datasets.source_type`` (Requirement 8.8); and
2. the ``normalized_url`` unique partial index **still only covers ``url``
   rows** — two ``html_upload`` rows with the *same* ``normalized_url`` are
   allowed (Requirement 8.11), while two ``url`` rows with the same
   ``normalized_url`` still collide.

This drives the actual migration (not ``Base.metadata.create_all``) so the
schema under test is exactly what the RDS Data API / psycopg path applies in
production. It runs under ``make test-int`` (``pytest -m integration``) with the
Compose stack up; it skips when no PostgreSQL is reachable or when configured
for the AWS Data API path.

The module owns a dedicated schema lifecycle: it drops any existing ``datasets``
schema objects, stamps the base clean, upgrades to head, runs its assertions,
then restores the ORM schema so sibling integration modules are unaffected.
Every row it inserts is rolled back or deleted, leaving no data behind.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from app.core import db as core_db
from app.core.config import get_settings
from app.db.models import Base
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _BACKEND_ROOT / "app" / "db" / "migrations" / "alembic.ini"


def _alembic_config() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "app" / "db" / "migrations"))
    return cfg


def _database_reachable() -> bool:
    try:
        with core_db.get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any connect/config failure means "skip"
        return False


def _drop_schema() -> None:
    """Drop the datasets schema objects (tables + the native enum types)."""
    engine = core_db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS dataset_versions CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS datasets CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
        conn.execute(text("DROP TYPE IF EXISTS dataset_status CASCADE"))
        conn.execute(text("DROP TYPE IF EXISTS source_type CASCADE"))


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema() -> Iterator[None]:
    """Apply the real migration chain to head, restore the ORM schema after."""
    get_settings.cache_clear()
    core_db.reset_engine()
    if get_settings().is_aws or not _database_reachable():
        pytest.skip("PostgreSQL not reachable; run under `make test-int` with the stack up")

    engine = core_db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))

    _drop_schema()
    # Run the migrations exactly as production would, through the same engine.
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command.upgrade(_alembic_config(), "head")

    try:
        yield
    finally:
        # Hand the shared DB back in the ORM-created state the other modules
        # expect, and forget the migrated engine.
        _drop_schema()
        Base.metadata.create_all(engine)
        core_db.reset_engine()


def _enum_values() -> list[str]:
    """Return the labels of the native ``source_type`` enum, in sort order."""
    with core_db.get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'source_type'"
            )
        ).fetchall()
    return sorted(r[0] for r in rows)


def test_migration_adds_html_upload_to_enum() -> None:
    """Requirement 8.8: the migrated enum carries all three values."""
    assert _enum_values() == ["html_upload", "upload", "url"]


def test_html_upload_value_accepted_on_insert() -> None:
    """Requirement 8.8: an ``html_upload`` row inserts cleanly."""
    ds_id = str(uuid.uuid4())
    with core_db.session_scope() as session:
        session.execute(
            text(
                "INSERT INTO datasets (id, name, source_type, status, data_version) "
                "VALUES (CAST(:id AS uuid), :name, 'html_upload', 'requested', 1)"
            ).bindparams(id=ds_id, name="html-upload-insert")
        )
    try:
        with core_db.get_engine().connect() as conn:
            stored = conn.execute(
                text("SELECT source_type FROM datasets WHERE id = CAST(:id AS uuid)").bindparams(
                    id=ds_id
                )
            ).scalar_one()
        assert stored == "html_upload"
    finally:
        with core_db.session_scope() as session:
            session.execute(
                text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)").bindparams(id=ds_id)
            )


def test_unique_index_still_covers_only_url_rows() -> None:
    """Requirement 8.11: dedupe covers ``url`` rows only.

    Two ``html_upload`` rows sharing a ``normalized_url`` are allowed (the
    partial index's ``WHERE source_type = 'url'`` excludes them), while two
    ``url`` rows with the same ``normalized_url`` still collide on the unique
    partial index.
    """
    shared_url = f"https://example.com/reviews/{uuid.uuid4()}"
    html_a = str(uuid.uuid4())
    html_b = str(uuid.uuid4())
    url_a = str(uuid.uuid4())
    url_b = str(uuid.uuid4())
    created: list[str] = []

    def _insert(ds_id: str, src: str) -> None:
        with core_db.session_scope() as session:
            session.execute(
                text(
                    "INSERT INTO datasets "
                    "(id, name, source_type, normalized_url, status, data_version) "
                    "VALUES (CAST(:id AS uuid), :name, "
                    "CAST(:src AS source_type), :url, 'requested', 1)"
                ).bindparams(id=ds_id, name="dedupe-probe", src=src, url=shared_url)
            )
        created.append(ds_id)

    try:
        # Two html_upload rows with the SAME normalized_url: both allowed.
        _insert(html_a, "html_upload")
        _insert(html_b, "html_upload")

        with core_db.get_engine().connect() as conn:
            html_count = conn.execute(
                text(
                    "SELECT count(*) FROM datasets "
                    "WHERE normalized_url = :url AND source_type = 'html_upload'"
                ).bindparams(url=shared_url)
            ).scalar_one()
        assert html_count == 2

        # First url row: allowed. Second url row with the same normalized_url:
        # rejected by the unique partial index.
        _insert(url_a, "url")
        with pytest.raises(IntegrityError):
            _insert(url_b, "url")
    finally:
        with core_db.session_scope() as session:
            for ds_id in created:
                session.execute(
                    text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)").bindparams(id=ds_id)
                )
