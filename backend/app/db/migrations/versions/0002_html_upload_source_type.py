"""Add the ``html_upload`` value to the ``source_type`` enum.

Revision ID: 0002_html_upload_source_type
Revises: 0001_datasets_and_versions
Create Date: 2024-01-02 00:00:00.000000

dataset-ingestion task 15. The native PostgreSQL ``source_type`` enum, created
by revision ``0001`` with the values ``url`` (URL check) and ``upload``
(tabular CSV), gains a **third** value ``html_upload`` for datasets created from
a saved page (Requirement 8.8).

Design note (``source_type`` gains an ``html_upload`` value): this is one
Alembic revision (one revision per schema-changing task, per the repository
conventions) that extends the native enum type and is applied through
``core.db`` / the RDS Data API like every other migration.

The unique partial index on ``normalized_url`` is deliberately **left
unchanged** at ``WHERE source_type = 'url'``, so ``html_upload`` rows are not
covered by URL dedupe (Requirement 8.11): an HTML upload without a source URL
has a null ``normalized_url`` and never collides, and multiple such uploads of
the same page are allowed. Routing an HTML upload *with* an already-tracked
source URL to the Refresh Service happens in ``add_items`` before any insert
(Requirement 8.12), not through this index.

``ALTER TYPE ... ADD VALUE`` only appends a label to the enum; it uses no
existing data and so runs inside the migration transaction on PostgreSQL 12+
and auto-commits under the RDS Data API. ``IF NOT EXISTS`` keeps the upgrade
idempotent. There is no safe, non-destructive downgrade: PostgreSQL cannot drop
a value from an enum type, so ``downgrade`` is a documented no-op.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_html_upload_source_type"
down_revision: str | Sequence[str] | None = "0001_datasets_and_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``html_upload`` to the native ``source_type`` enum."""
    op.execute("ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'html_upload'")


def downgrade() -> None:
    """No-op: PostgreSQL cannot remove a value from an enum type.

    Dropping ``html_upload`` would require recreating the enum and rewriting
    every ``datasets.source_type`` value, which is destructive and never needed
    for a forward-only schema change. The downgrade is intentionally empty.
    """
    # Intentionally left blank - see the module docstring.
