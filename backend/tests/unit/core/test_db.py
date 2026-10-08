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


# ---------------------------------------------------------------------------
# Aurora Serverless v2 resume-retry warm-up (AWS mode only)
# ---------------------------------------------------------------------------


class _ResumingError(Exception):
    """Stand-in whose repr contains DatabaseResumingException."""

    def __repr__(self) -> str:
        return "botocore.errorfactory.DatabaseResumingException('resuming')"


class _FakeConnection:
    """Minimal connection: execute() raises `fail_times` resume errors first."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.execute_calls = 0
        self.rollback_calls = 0

    def execute(self, _stmt: object) -> None:
        self.execute_calls += 1
        if self.execute_calls <= self.fail_times:
            raise _ResumingError()

    def rollback(self) -> None:
        self.rollback_calls += 1


def test_resume_retry_installed_only_in_aws_mode() -> None:
    """create_db_engine registers the engine_connect warm-up only in AWS mode."""
    aws_engine = db.create_db_engine(_aws_settings())
    assert aws_engine.dispatch.engine_connect  # has listeners

    local_engine = db.create_db_engine(_local_settings())
    assert not local_engine.dispatch.engine_connect  # none registered


def test_warm_up_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The warm-up retries on DatabaseResumingException then returns cleanly."""
    sleeps: list[float] = []
    monkeypatch.setattr(db.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(db, "_RESUME_MAX_ATTEMPTS", 5)

    engine = db.create_db_engine(_aws_settings())
    conn = _FakeConnection(fail_times=2)
    engine.dispatch.engine_connect(conn)

    assert conn.execute_calls == 3  # 2 failures + 1 success
    assert sleeps == [db._RESUME_WAIT_SECONDS, db._RESUME_WAIT_SECONDS]


def test_warm_up_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cluster that never resumes raises _ResumingError after bounded attempts."""
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)
    monkeypatch.setattr(db, "_RESUME_MAX_ATTEMPTS", 3)

    engine = db.create_db_engine(_aws_settings())
    conn = _FakeConnection(fail_times=99)
    with pytest.raises(_ResumingError):
        engine.dispatch.engine_connect(conn)
    assert conn.execute_calls == 3


def test_warm_up_reraises_non_resume_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-resume error is raised immediately, not retried."""
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)

    class _OtherConn(_FakeConnection):
        def execute(self, _stmt: object) -> None:
            self.execute_calls += 1
            raise ValueError("not a resume")

    engine = db.create_db_engine(_aws_settings())
    conn = _OtherConn(fail_times=0)
    with pytest.raises(ValueError, match="not a resume"):
        engine.dispatch.engine_connect(conn)
    assert conn.execute_calls == 1  # no retry
