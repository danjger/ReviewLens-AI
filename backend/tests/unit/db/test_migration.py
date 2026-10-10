"""Unit tests for the first Alembic migration.

Verifies the revision chain and that the migration's offline SQL (emitted
without a live connection) produces the tables, enum, JSONB columns, and
indexes required by Requirements 4.2, 4.3, and 4.6.

These tests drive Alembic in offline (``--sql``) mode so no database is
required.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _BACKEND_ROOT / "app" / "db" / "migrations" / "alembic.ini"


def _alembic_config() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    # alembic.ini uses a relative script_location resolved from CWD; pin it to
    # an absolute path so the test is independent of the working directory.
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "app" / "db" / "migrations"))
    return cfg


def test_single_head_revision() -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    heads = script.get_heads()
    assert heads == ["0002_html_upload_source_type"]


def test_base_migration_has_no_down_revision() -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    rev = script.get_revision("0001_datasets_and_versions")
    assert rev.down_revision is None


def test_html_upload_revision_follows_base() -> None:
    """The ``html_upload`` enum revision chains directly off the base one.

    One Alembic revision per schema-changing task (dataset-ingestion task 15):
    the second revision's down_revision is the first.
    """
    script = ScriptDirectory.from_config(_alembic_config())
    rev = script.get_revision("0002_html_upload_source_type")
    assert rev.down_revision == "0001_datasets_and_versions"


def test_offline_upgrade_emits_expected_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Render the migration to SQL and assert the key DDL is present.

    Uses Alembic's offline mode via ``command.upgrade(..., sql=True)`` and
    captures the emitted statements. A dummy ``DATABASE_URL`` is enough because
    offline mode never connects.
    """
    import io
    from contextlib import redirect_stdout

    from alembic import command
    from app.core import db as core_db
    from app.core.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/reviewlens")
    monkeypatch.setenv("DB_RESOURCE_ARN", "")
    monkeypatch.setenv("DB_SECRET_ARN", "")
    get_settings.cache_clear()
    core_db.reset_engine()

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command.upgrade(_alembic_config(), "head", sql=True)
    sql = buffer.getvalue()

    # Enum type + the four allowed status values. Revision 0001 creates the
    # source_type enum with just the two original values; revision 0002 then
    # appends the third (html_upload) via ALTER TYPE (task 15, Requirement 8.8).
    assert "CREATE TYPE source_type AS ENUM ('url', 'upload')" in sql
    assert "ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'html_upload'" in sql
    assert (
        "CREATE TYPE dataset_status AS ENUM ('requested', 'processing', 'updated', 'failed')" in sql
    )
    # Tables.
    assert "CREATE TABLE datasets" in sql
    assert "CREATE TABLE dataset_versions" in sql
    # JSONB columns and the version counters.
    assert "status_detail JSONB" in sql
    assert "metrics JSONB" in sql
    assert "data_version INTEGER" in sql
    assert "active_version INTEGER" in sql
    # The check constraint pinning the status values.
    assert "ck_datasets_status_allowed_values" in sql
    # Indexes, including the unique partial index on normalized_url.
    assert "ix_datasets_archived_updated" in sql
    assert (
        "CREATE UNIQUE INDEX uq_datasets_normalized_url ON datasets "
        "(normalized_url) WHERE source_type = 'url'" in sql
    )
    assert "ix_datasets_normalized_final_url" in sql
    assert "ix_datasets_status_updated" in sql

    get_settings.cache_clear()
    core_db.reset_engine()
