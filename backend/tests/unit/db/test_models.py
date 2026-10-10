"""Unit tests for the SQLAlchemy models (app.db.models).

These assert the schema shape described by the design's Data Models section and
Requirements 4.2, 4.3, 4.6: the ``datasets`` and ``dataset_versions`` columns,
the native status enum constrained to the four allowed values, the JSONB
columns, ``data_version`` / ``active_version``, the normalized URL columns, and
the required indexes (including the unique partial index on ``normalized_url``).

No database connection is made; the tests inspect SQLAlchemy metadata and the
compiled DDL for the PostgreSQL dialect.
"""

from __future__ import annotations

from app.db.models import (
    Base,
    Dataset,
    DatasetStatus,
    DatasetVersion,
    SourceType,
)
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------


def test_status_enum_has_exactly_four_values() -> None:
    assert {s.value for s in DatasetStatus} == {
        "requested",
        "processing",
        "updated",
        "failed",
    }


def test_source_type_enum_values() -> None:
    # dataset-ingestion task 15 (Requirement 8.8): the enum gains a third value
    # ``html_upload`` for datasets created from a saved page.
    assert {s.value for s in SourceType} == {"url", "upload", "html_upload"}


# ---------------------------------------------------------------------------
# datasets columns (Requirement 4.2)
# ---------------------------------------------------------------------------

_EXPECTED_DATASET_COLUMNS = {
    "id",
    "name",
    "page_title",
    "source_type",
    "original_url",
    "final_url",
    "normalized_url",
    "normalized_final_url",
    "platform",
    "status",
    "status_detail",
    "requested_at",
    "updated_at",
    "archived_at",
    "metrics",
    "data_version",
    "active_version",
}


def test_datasets_has_all_required_columns() -> None:
    assert set(Dataset.__table__.columns.keys()) == _EXPECTED_DATASET_COLUMNS


def test_data_version_not_null_and_active_version_nullable() -> None:
    cols = Dataset.__table__.columns
    assert cols["data_version"].nullable is False
    assert cols["active_version"].nullable is True


def test_status_detail_and_metrics_are_jsonb() -> None:
    cols = Dataset.__table__.columns
    assert isinstance(cols["status_detail"].type, postgresql.JSONB)
    assert isinstance(cols["metrics"].type, postgresql.JSONB)
    # status_detail is required (append target); metrics is written later.
    assert cols["status_detail"].nullable is False
    assert cols["metrics"].nullable is True


def test_normalized_url_columns_present_and_nullable() -> None:
    cols = Dataset.__table__.columns
    assert cols["normalized_url"].nullable is True
    assert cols["normalized_final_url"].nullable is True


# ---------------------------------------------------------------------------
# status enum constraint (Requirement 4.3)
# ---------------------------------------------------------------------------


def test_status_check_constraint_pins_four_values() -> None:
    checks = [c for c in Dataset.__table__.constraints if isinstance(c, CheckConstraint)]
    assert len(checks) == 1
    sqltext = str(checks[0].sqltext)
    for value in ("requested", "processing", "updated", "failed"):
        assert value in sqltext


def test_status_column_uses_native_enum() -> None:
    status_type = Dataset.__table__.columns["status"].type
    # The native PG enum carries the four values and a stable type name.
    assert sorted(status_type.enums) == ["failed", "processing", "requested", "updated"]
    assert status_type.name == "dataset_status"


# ---------------------------------------------------------------------------
# indexes (Requirement 4.2)
# ---------------------------------------------------------------------------


def test_required_indexes_exist() -> None:
    index_names = {ix.name for ix in Dataset.__table__.indexes}
    assert index_names == {
        "ix_datasets_archived_updated",
        "uq_datasets_normalized_url",
        "ix_datasets_normalized_final_url",
        "ix_datasets_status_updated",
    }


def test_normalized_url_index_is_unique_and_partial() -> None:
    ix = next(ix for ix in Dataset.__table__.indexes if ix.name == "uq_datasets_normalized_url")
    assert ix.unique is True
    # Partial on URL datasets only, so uploads never collide on NULL URLs.
    where = ix.dialect_options["postgresql"]["where"]
    assert "source_type = 'url'" in str(where)


# ---------------------------------------------------------------------------
# dataset_versions (Requirement 4.6)
# ---------------------------------------------------------------------------

_EXPECTED_VERSION_COLUMNS = {
    "dataset_id",
    "version",
    "trigger",
    "requested_at",
    "completed_at",
    "review_count",
    "extraction_method",
    "outcome",
}


def test_dataset_versions_columns() -> None:
    assert set(DatasetVersion.__table__.columns.keys()) == _EXPECTED_VERSION_COLUMNS


def test_dataset_versions_composite_pk() -> None:
    pk_cols = [c.name for c in DatasetVersion.__table__.primary_key.columns]
    assert pk_cols == ["dataset_id", "version"]


def test_dataset_versions_fk_to_datasets() -> None:
    fks = list(DatasetVersion.__table__.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column is Dataset.__table__.columns["id"]


# ---------------------------------------------------------------------------
# DDL compiles for PostgreSQL
# ---------------------------------------------------------------------------


def test_tables_compile_for_postgresql() -> None:
    dialect = postgresql.dialect()
    datasets_ddl = str(CreateTable(Dataset.__table__).compile(dialect=dialect))
    versions_ddl = str(CreateTable(DatasetVersion.__table__).compile(dialect=dialect))
    assert "JSONB" in datasets_ddl
    assert "data_version" in datasets_ddl
    assert "active_version" in datasets_ddl
    assert "dataset_id" in versions_ddl


def test_shared_base_metadata_holds_both_tables() -> None:
    assert {"datasets", "dataset_versions"} <= set(Base.metadata.tables)
