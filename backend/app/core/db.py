"""Relational data layer: one SQLAlchemy engine factory for both compute modes.

ReviewLens AI reaches the database only through this module. The rest of the
code asks for a :class:`~sqlalchemy.orm.Session` and never cares which driver
is underneath:

- **AWS** (Aurora PostgreSQL Serverless v2): the ``sqlalchemy-aurora-data-api``
  dialect talks to the RDS Data API over HTTPS. There is no VPC, no NAT gateway,
  and no per-instance connection pool to exhaust when Services scale out.
- **Local / CI**: plain ``psycopg`` against the PostgreSQL 16 container.

Driver selection is based on :class:`app.core.config.Settings`: when both
``DB_RESOURCE_ARN`` and ``DB_SECRET_ARN`` are set (``settings.is_aws``) the Data
API driver is used; otherwise ``DATABASE_URL`` is used with psycopg.

Usage::

    from app.core.db import session_scope

    with session_scope() as session:
        session.execute(text("SELECT 1"))

The engine and sessionmaker are built once per process and memoised. Call
``reset_engine()`` in tests after changing settings.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import URL, create_engine, make_url
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL / connect-args construction (pure, unit-testable)
# ---------------------------------------------------------------------------

# SQLAlchemy dialect strings for the two drivers.
_AURORA_DIALECT = "postgresql+auroradataapi"
_PSYCOPG_DIALECT = "postgresql+psycopg"


def build_engine_url(settings: Settings) -> URL:
    """Return the SQLAlchemy URL for the active driver.

    In AWS mode the Data API carries the connection details in ``connect_args``
    (see :func:`build_connect_args`), so the URL only needs the dialect and the
    database (schema) name. In local mode the psycopg ``DATABASE_URL`` is used,
    normalised to the ``postgresql+psycopg`` dialect so the driver is explicit
    regardless of how the URL was written.

    Raises ``RuntimeError`` when the required configuration for the selected
    mode is missing.
    """
    if settings.is_aws:
        if not settings.db_database_name:
            raise RuntimeError("DB_DATABASE_NAME must be set in Data API mode")
        # aurora-data-api takes the ARNs via connect_args; the host is unused.
        return URL.create(
            drivername=_AURORA_DIALECT,
            database=settings.db_database_name,
        )

    if not settings.database_url:
        raise RuntimeError(
            "DATABASE_URL must be set when not running in Data API mode "
            "(DB_RESOURCE_ARN and DB_SECRET_ARN are unset)"
        )
    return _normalise_psycopg_url(settings.database_url)


def _normalise_psycopg_url(database_url: str) -> URL:
    """Coerce any PostgreSQL URL to the explicit ``postgresql+psycopg`` dialect.

    Accepts bare ``postgresql://`` and ``postgres://`` URLs as well as URLs that
    already name a driver, and always returns one that uses psycopg so the
    driver can't be ambiguous or accidentally resolve to psycopg2.
    """
    url = make_url(database_url)
    backend = url.get_backend_name()
    if backend not in ("postgresql", "postgres"):
        raise RuntimeError(f"DATABASE_URL must be a PostgreSQL URL, got backend {backend!r}")
    return url.set(drivername=_PSYCOPG_DIALECT)


def build_connect_args(settings: Settings) -> dict[str, Any]:
    """Return driver-specific ``connect_args`` for :func:`create_engine`.

    In AWS mode this supplies the RDS Data API coordinates (cluster ARN, secret
    ARN, and database name) to the ``aurora-data-api`` driver. In local mode no
    extra arguments are needed.
    """
    if settings.is_aws:
        connect_args: dict[str, Any] = {
            "aurora_cluster_arn": settings.db_resource_arn,
            "secret_arn": settings.db_secret_arn,
            "database": settings.db_database_name,
        }
        if settings.aws_endpoint_url:
            # LocalStack / integration override: the aurora-data-api driver takes
            # a preconfigured boto3 rds-data client rather than an endpoint URL.
            import boto3

            connect_args["rds_data_client"] = boto3.client(
                "rds-data", endpoint_url=settings.aws_endpoint_url
            )
        return connect_args
    return {}


# ---------------------------------------------------------------------------
# Engine / session factory (memoised per process)
# ---------------------------------------------------------------------------

_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def create_db_engine(settings: Settings | None = None) -> Engine:
    """Build a fresh SQLAlchemy :class:`Engine` for the active driver.

    This does not cache; use :func:`get_engine` for the shared process-wide
    engine. The Data API holds no persistent connections, so pooling is
    irrelevant there; locally the default pool is used.
    """
    settings = settings or get_settings()
    url = build_engine_url(settings)
    connect_args = build_connect_args(settings)
    logger.info("Creating database engine (dialect=%s)", url.drivername)
    return create_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,
        future=True,
    )


def get_engine() -> Engine:
    """Return the shared process-wide engine, building it on first use."""
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    """Return the shared session factory, building it on first use."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(
            bind=get_engine(),
            class_=Session,
            expire_on_commit=False,
            future=True,
        )
    return _sessionmaker


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a session that commits on success and rolls back on error.

    This is the primary entry point for database work::

        with session_scope() as session:
            session.add(obj)
    """
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Dispose and clear the cached engine and sessionmaker.

    Intended for tests that change settings between cases. Safe to call when no
    engine has been created yet.
    """
    global _engine, _sessionmaker
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessionmaker = None
