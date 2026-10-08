"""SQLAlchemy ORM models for ReviewLens AI.

This module defines the relational schema that is the single source of truth
for dataset records (Requirement 4). The two tables are:

- :class:`Dataset` – one row per ingested set of reviews (from a URL or an
  uploaded file), with its current ``status`` and an append-only
  ``status_detail`` event log.
- :class:`DatasetVersion` – one row per *data version* of a dataset, recording
  what triggered it, when it was requested and completed, its review count, the
  extraction method, and the outcome.

Models use SQLAlchemy 2 declarative style with ``Mapped[]`` typing so the whole
module passes ``mypy --strict``. The ``Base`` declared here owns a shared
``MetaData`` with an explicit naming convention, which gives constraints and
indexes deterministic names so Alembic autogenerate stays stable.

The status enum is constrained at the database level to the four allowed
values (Requirement 4.3). Index definitions match the design's Data Models
section, including the unique partial index on ``normalized_url`` for URL
datasets (Requirement 4.2, dataset-ingestion task 5).
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime

# ---------------------------------------------------------------------------
# Declarative base with an explicit naming convention
# ---------------------------------------------------------------------------

# A naming convention makes constraint/index names deterministic across runs so
# that Alembic autogenerate produces stable, reviewable migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model and Alembic."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SourceType(enum.StrEnum):
    """How a dataset's reviews were obtained."""

    URL = "url"
    UPLOAD = "upload"


class DatasetStatus(enum.StrEnum):
    """Lifecycle status of a dataset.

    Only these four values are ever stored (Requirement 4.3); the database
    enforces this with a native PostgreSQL enum type.
    """

    REQUESTED = "requested"
    PROCESSING = "processing"
    UPDATED = "updated"
    FAILED = "failed"


# Native PostgreSQL enum types. ``create_type=False`` on the column usages keeps
# a single shared type; we let the first table create it and reuse by name.
_source_type_enum = Enum(
    SourceType,
    name="source_type",
    values_callable=lambda e: [member.value for member in e],
)
_dataset_status_enum = Enum(
    DatasetStatus,
    name="dataset_status",
    values_callable=lambda e: [member.value for member in e],
)


# ---------------------------------------------------------------------------
# datasets
# ---------------------------------------------------------------------------


class Dataset(Base):
    """One ingested set of reviews, identified by a unique UUID.

    Columns mirror the design's ``datasets`` table (Requirement 4.2). The
    ``status`` column is constrained to the four allowed values (Requirement
    4.3), and ``status_detail`` holds the append-only event log written by
    ``db.status.transition()`` / ``db.status.log_event()`` (Requirement 4.4).
    """

    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    page_title: Mapped[str | None] = mapped_column(String, nullable=True)

    source_type: Mapped[SourceType] = mapped_column(_source_type_enum, nullable=False)

    original_url: Mapped[str | None] = mapped_column(String, nullable=True)
    final_url: Mapped[str | None] = mapped_column(String, nullable=True)
    normalized_url: Mapped[str | None] = mapped_column(String, nullable=True)
    normalized_final_url: Mapped[str | None] = mapped_column(String, nullable=True)

    platform: Mapped[str | None] = mapped_column(String, nullable=True)

    status: Mapped[DatasetStatus] = mapped_column(_dataset_status_enum, nullable=False)

    # Ordered event log + redirect/viability detail. Defaults to an empty event
    # list so appends never have to special-case a NULL.
    status_detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{\"events\": []}'::jsonb"),
    )

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Written by review-analysis for the active version only.
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Latest version attempted; latest version that finished `updated`.
    data_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    active_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    versions: Mapped[list[DatasetVersion]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # The status enum is enforced by the native type above; this redundant
        # check keeps the constraint explicit and portable across dialects.
        CheckConstraint(
            "status IN ('requested', 'processing', 'updated', 'failed')",
            name="status_allowed_values",
        ),
        # Library listing: non-archived first, newest update first.
        Index("ix_datasets_archived_updated", "archived_at", text("updated_at DESC")),
        # A URL dataset is never duplicated: one row per normalized URL.
        Index(
            "uq_datasets_normalized_url",
            "normalized_url",
            unique=True,
            postgresql_where=text("source_type = 'url'"),
        ),
        # Resolve a dataset by the URL a redirect landed on.
        Index("ix_datasets_normalized_final_url", "normalized_final_url"),
        # The sweeper scans by status and staleness.
        Index("ix_datasets_status_updated", "status", "updated_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<Dataset id={self.id!r} status={self.status!r} name={self.name!r}>"


# ---------------------------------------------------------------------------
# dataset_versions
# ---------------------------------------------------------------------------


class DatasetVersion(Base):
    """One data version of a dataset.

    The base columns are implemented here (Requirement 4.6); further columns
    are detailed in the ``dataset-ingestion`` spec. Written by the Refresh
    Service and ingestion, completed by review-analysis, and read by the Library
    and chat refresh markers.
    """

    __tablename__ = "dataset_versions"

    dataset_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)

    # What started this version, e.g. "add", "refresh", "sweeper", "upload".
    trigger: Mapped[str] = mapped_column(String, nullable=False)

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    review_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # How the reviews were extracted, e.g. "structured", "ai", "upload".
    extraction_method: Mapped[str | None] = mapped_column(String, nullable=True)

    # Terminal outcome, e.g. "updated" or "failed"; null while in flight.
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)

    dataset: Mapped[Dataset] = relationship(back_populates="versions")

    __table_args__ = (
        ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="dataset_versions_dataset_id_datasets",
            ondelete="CASCADE",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"<DatasetVersion dataset_id={self.dataset_id!r} "
            f"version={self.version!r} outcome={self.outcome!r}>"
        )
