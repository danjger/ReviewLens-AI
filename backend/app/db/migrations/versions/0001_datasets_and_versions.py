"""Create datasets and dataset_versions tables.

Revision ID: 0001_datasets_and_versions
Revises:
Create Date: 2024-01-01 00:00:00.000000

First migration for the ReviewLens AI relational store (platform-foundation
task 5.2). Creates the ``datasets`` and ``dataset_versions`` tables with:

- native PostgreSQL enum types for ``source_type`` and ``status``, plus a
  redundant CHECK constraint pinning the four allowed status values
  (Requirement 4.3);
- JSONB ``status_detail`` and ``metrics`` columns (Requirement 4.2);
- ``data_version`` and ``active_version`` columns (Requirement 4.2);
- the normalized URL columns and the required indexes, including the unique
  partial index on ``normalized_url`` for URL datasets (Requirements 4.2, 4.6).

``gen_random_uuid()`` requires the ``pgcrypto`` extension, which is enabled
here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_datasets_and_versions"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # gen_random_uuid() lives in pgcrypto on PostgreSQL < 13 and is built in on
    # 13+. Enable the extension so the server-side default works everywhere.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    source_type = postgresql.ENUM("url", "upload", name="source_type", create_type=True)
    dataset_status = postgresql.ENUM(
        "requested",
        "processing",
        "updated",
        "failed",
        name="dataset_status",
        create_type=True,
    )
    source_type.create(op.get_bind(), checkfirst=True)
    dataset_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "datasets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("page_title", sa.String(), nullable=True),
        sa.Column(
            "source_type",
            postgresql.ENUM("url", "upload", name="source_type", create_type=False),
            nullable=False,
        ),
        sa.Column("original_url", sa.String(), nullable=True),
        sa.Column("final_url", sa.String(), nullable=True),
        sa.Column("normalized_url", sa.String(), nullable=True),
        sa.Column("normalized_final_url", sa.String(), nullable=True),
        sa.Column("platform", sa.String(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "requested",
                "processing",
                "updated",
                "failed",
                name="dataset_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{\"events\": []}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "data_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("active_version", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN ('requested', 'processing', 'updated', 'failed')",
            name="status_allowed_values",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_datasets"),
    )

    op.create_index(
        "ix_datasets_archived_updated",
        "datasets",
        ["archived_at", sa.text("updated_at DESC")],
        unique=False,
    )
    op.create_index(
        "uq_datasets_normalized_url",
        "datasets",
        ["normalized_url"],
        unique=True,
        postgresql_where=sa.text("source_type = 'url'"),
    )
    op.create_index(
        "ix_datasets_normalized_final_url",
        "datasets",
        ["normalized_final_url"],
        unique=False,
    )
    op.create_index(
        "ix_datasets_status_updated",
        "datasets",
        ["status", "updated_at"],
        unique=False,
    )

    op.create_table(
        "dataset_versions",
        sa.Column("dataset_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=True),
        sa.Column("extraction_method", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="dataset_versions_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("dataset_id", "version", name="pk_dataset_versions"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("dataset_versions")
    op.drop_index("ix_datasets_status_updated", table_name="datasets")
    op.drop_index("ix_datasets_normalized_final_url", table_name="datasets")
    op.drop_index(
        "uq_datasets_normalized_url",
        table_name="datasets",
        postgresql_where=sa.text("source_type = 'url'"),
    )
    op.drop_index("ix_datasets_archived_updated", table_name="datasets")
    op.drop_table("datasets")

    postgresql.ENUM(name="dataset_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="source_type").drop(op.get_bind(), checkfirst=True)
