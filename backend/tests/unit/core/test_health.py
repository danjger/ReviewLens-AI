"""Unit tests for app.core.health.

Tests liveness, readiness response shapes, and the consumer health listener.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from app.core.health import liveness, readiness, start_consumer_health_listener

# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


def test_liveness_returns_200() -> None:
    response = liveness()
    assert response.status_code == 200


def test_liveness_body_is_ok() -> None:
    import json

    response = liveness()
    body = json.loads(response.body)
    assert body == {"status": "ok"}


# ---------------------------------------------------------------------------
# Readiness – success path
# ---------------------------------------------------------------------------


def test_readiness_ok_when_db_and_s3_reachable() -> None:
    with (
        patch("app.core.health._check_database", return_value=None),
        patch("app.core.health._check_s3", return_value=None),
    ):
        response = readiness(database_url="postgresql://x", s3_bucket="my-bucket")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Readiness – failure paths
# ---------------------------------------------------------------------------


def test_readiness_503_when_db_unreachable() -> None:
    with (
        patch("app.core.health._check_database", return_value="connection refused"),
        patch("app.core.health._check_s3", return_value=None),
    ):
        response = readiness(database_url="postgresql://x", s3_bucket="my-bucket")
    assert response.status_code == 503


def test_readiness_503_when_s3_unreachable() -> None:
    with (
        patch("app.core.health._check_database", return_value=None),
        patch("app.core.health._check_s3", return_value="no such bucket"),
    ):
        response = readiness(database_url="postgresql://x", s3_bucket="my-bucket")
    assert response.status_code == 503


def test_readiness_503_when_database_url_missing() -> None:
    """readiness() must fail if DATABASE_URL is not configured."""
    with patch("app.core.health._check_s3", return_value=None):
        response = readiness(database_url=None, s3_bucket="my-bucket")
    assert response.status_code == 503


def test_check_database_aws_mode_probes_engine_not_database_url() -> None:
    """In Data API (AWS) mode, a missing DATABASE_URL must NOT fail readiness.

    AWS mode reaches the DB through the RDS Data API, so the probe runs
    SELECT 1 via the shared engine rather than requiring DATABASE_URL.
    """
    from unittest.mock import MagicMock

    from app.core.health import _check_database

    settings = MagicMock()
    settings.is_aws = True
    engine = MagicMock()
    conn_cm = engine.connect.return_value
    conn_cm.__enter__.return_value = MagicMock()

    with (
        patch("app.core.config.get_settings", return_value=settings),
        patch("app.core.db.get_engine", return_value=engine),
    ):
        assert _check_database(None) is None
    conn_cm.__enter__.return_value.execute.assert_called_once()


def test_check_database_aws_mode_reports_engine_failure() -> None:
    """In Data API mode, an engine failure is reported as unreachable."""
    from unittest.mock import MagicMock

    from app.core.health import _check_database

    settings = MagicMock()
    settings.is_aws = True
    engine = MagicMock()
    engine.connect.side_effect = RuntimeError("data api down")

    with (
        patch("app.core.config.get_settings", return_value=settings),
        patch("app.core.db.get_engine", return_value=engine),
    ):
        err = _check_database(None)
    assert err is not None
    assert "database unreachable" in err


def test_readiness_503_when_s3_bucket_missing() -> None:
    """readiness() must fail if S3_BUCKET is not configured."""
    with patch("app.core.health._check_database", return_value=None):
        response = readiness(database_url="postgresql://x", s3_bucket=None)
    assert response.status_code == 503


def test_readiness_503_body_contains_reason() -> None:
    import json

    with patch("app.core.health._check_database", return_value="timeout"):
        response = readiness(database_url="postgresql://x", s3_bucket="bucket")
    body = json.loads(response.body)
    assert body["status"] == "not ready"
    assert "timeout" in body["reason"]


# ---------------------------------------------------------------------------
# Consumer health listener
# ---------------------------------------------------------------------------


def test_consumer_health_listener_serves_healthz() -> None:
    """The consumer health listener must respond 200 to GET /healthz."""
    import socket
    import urllib.request

    # Pick a random free port
    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    start_consumer_health_listener(port=port)
    # Give the daemon thread a moment to bind
    time.sleep(0.2)

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz") as resp:
        assert resp.status == 200
        body = resp.read()
    assert b'"ok"' in body


def test_consumer_health_listener_serves_readyz() -> None:
    """The consumer health listener must respond 200 to GET /readyz."""
    import socket
    import urllib.request

    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    start_consumer_health_listener(port=port)
    time.sleep(0.2)

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz") as resp:
        assert resp.status == 200


def test_consumer_health_listener_404_for_unknown_path() -> None:
    """The consumer health listener must return 404 for unknown paths."""
    import socket
    import urllib.error
    import urllib.request

    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    start_consumer_health_listener(port=port)
    time.sleep(0.2)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/unknown")
    assert exc_info.value.code == 404
