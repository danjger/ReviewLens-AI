"""Alembic migration environment for ReviewLens AI.

Target metadata comes from :data:`app.db.models.Base`, and the database
connection comes from :mod:`app.core.db` so migrations use the exact same
engine factory (and driver selection) as the running application. Locally and
in CI this is psycopg against PostgreSQL; in AWS it is the RDS Data API driver.

Both offline (``--sql``) and online modes are supported.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from app.core.db import create_db_engine
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode, emitting SQL without a DBAPI connection.

    The engine URL is derived from the application settings via
    :func:`app.core.db.create_db_engine` so offline SQL matches the configured
    driver/dialect.
    """
    engine = create_db_engine()
    context.configure(
        url=engine.url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    engine = create_db_engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
