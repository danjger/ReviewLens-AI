"""Unit tests for app.core.db.

Covers the driver / URL / connect-args selection logic that decides between the
RDS Data API driver (AWS) and psycopg (local), plus the engine/session factory
caching and reset behaviour. No real database connection is made.
"""

from __future__ import annotations

import pytest
from app.core import db
from app.core.config import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _local_settings(url: str = "postgresql://user:pw@localhost:5432/reviewlens") -> Settings:
    return Settings(database_url=url, db_resource_arn="", db_secret_arn="")


def _aws_settings() -> Settings:
    return Settings(
        database_url="",
        db_resource_arn="arn:aws:rds:us-east-1:123456789012:cluster:reviewlens",
        db_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:db-abc",
        db_database_name="reviewlens",
    )


@pytest.fixture(autouse=True)
def _reset_engine() -> None:
    """Ensure every test starts and ends with no cached engine."""
    db.reset_engine()
    yield
    db.reset_engine()


# ---------------------------------------------------------------------------
# is_aws selection
# ---------------------------------------------------------------------------


def test_is_aws_true_when_both_arns_present() -> None:
    assert _aws_settings().is_aws is True


def test_is_aws_false_when_arns_missing() -> None:
    assert _local_settings().is_aws is False


# ---------------------------------------------------------------------------
# build_engine_url
# ---------------------------------------------------------------------------


def test_local_url_normalised_to_psycopg_dialect() -> None:
    url = db.build_engine_url(_local_settings())
    assert url.drivername == "postgresql+psycopg"
    assert url.database == "reviewlens"
    assert url.host == "localhost"


def test_local_url_already_naming_psycopg_is_preserved() -> None:
    url = db.build_engine_url(_local_settings("postgresql+psycopg://u:p@db:5432/reviewlens"))
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "db"


def test_local_url_postgres_scheme_accepted() -> None:
    url = db.build_engine_url(_local_settings("postgres://u:p@h:5432/reviewlens"))
    assert url.drivername == "postgresql+psycopg"


def test_local_url_rejects_non_postgres_backend() -> None:
    with pytest.raises(RuntimeError, match="PostgreSQL URL"):
        db.build_engine_url(_local_settings("mysql://u:p@h:3306/reviewlens"))


def test_local_url_missing_database_url_raises() -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        db.build_engine_url(_local_settings(""))


def test_aws_url_uses_aurora_dialect_and_database_name() -> None:
    url = db.build_engine_url(_aws_settings())
    assert url.drivername == "postgresql+auroradataapi"
    assert url.database == "reviewlens"


def test_aws_url_requires_database_name() -> None:
    settings = _aws_settings()
    settings.db_database_name = ""
    with pytest.raises(RuntimeError, match="DB_DATABASE_NAME"):
        db.build_engine_url(settings)


# ---------------------------------------------------------------------------
# build_connect_args
# ---------------------------------------------------------------------------


def test_local_connect_args_empty() -> None:
    assert db.build_connect_args(_local_settings()) == {}


def test_aws_connect_args_carry_arns() -> None:
    args = db.build_connect_args(_aws_settings())
    assert args["aurora_cluster_arn"].endswith(":cluster:reviewlens")
    assert args["secret_arn"].endswith(":secret:db-abc")
    assert args["database"] == "reviewlens"
    assert "rds_data_client" not in args


def test_aws_connect_args_endpoint_override_builds_client() -> None:
    settings = _aws_settings()
    settings.aws_endpoint_url = "http://localstack:4566"
    args = db.build_connect_args(settings)
    # A preconfigured rds-data client is supplied for LocalStack.
    assert "rds_data_client" in args
    assert "endpoint_url" not in args


# ---------------------------------------------------------------------------
# Engine / session factory caching
# ---------------------------------------------------------------------------


def test_create_db_engine_local_dialect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "get_settings", _local_settings)
    engine = db.create_db_engine()
    assert engine.dialect.name == "postgresql"
    engine.dispose()


def test_create_db_engine_aws_dialect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "get_settings", _aws_settings)
    engine = db.create_db_engine()
    # The aurora-data-api dialect reports the postgresql name with its own driver.
    assert engine.dialect.name == "postgresql"
    assert engine.url.drivername == "postgresql+auroradataapi"
    engine.dispose()


def test_get_engine_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "get_settings", _local_settings)
    first = db.get_engine()
    second = db.get_engine()
    assert first is second


def test_get_sessionmaker_bound_to_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "get_settings", _local_settings)
    maker = db.get_sessionmaker()
    assert maker is db.get_sessionmaker()
    assert maker.kw["bind"] is db.get_engine()


def test_reset_engine_rebuilds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "get_settings", _local_settings)
    first = db.get_engine()
    db.reset_engine()
    second = db.get_engine()
    assert first is not second
    second.dispose()


def test_reset_engine_safe_when_unbuilt() -> None:
    # Should not raise even though no engine exists yet.
    db.reset_engine()
