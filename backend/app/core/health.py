"""Health and readiness check helpers shared by all HTTP services.

/healthz  – liveness:   is the process alive and able to handle requests?
/readyz   – readiness:  is configuration loaded, and are the database and S3
            reachable?

Both endpoints are exempt from the origin-guard middleware (see
core.origin_guard) so that load-balancers and ECS health checks can reach
them without the CloudFront origin header.
"""

from __future__ import annotations

import logging
from typing import Any

import boto3
from fastapi import status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

_OK_BODY: dict[str, str] = {"status": "ok"}
_FAIL_PREFIX = "not ready"


def _ok() -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_200_OK, content=_OK_BODY)


def _fail(reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": _FAIL_PREFIX, "reason": reason},
    )


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


def liveness() -> JSONResponse:
    """Return 200 OK if the process is alive.

    This check is intentionally trivial – it only verifies the event loop is
    responsive.  It should never block or make network calls.
    """
    return _ok()


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def _check_database(database_url: str | None) -> str | None:
    """Return an error string if the database is unreachable, else None.

    In AWS (Data API) mode the DB is reached through the RDS Data API, not a
    ``DATABASE_URL`` connection string (which is intentionally unset), so probe
    via the shared SQLAlchemy engine — the same path the app uses. In local /
    container mode probe the ``DATABASE_URL`` with a minimal psycopg connection.
    """
    from app.core.config import get_settings

    if get_settings().is_aws:
        try:
            from sqlalchemy import text

            from app.core.db import get_engine

            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("readyz: Data API database probe failed: %s", exc)
            return f"database unreachable: {exc}"

    if not database_url:
        return "DATABASE_URL not configured"
    try:
        # Use a minimal synchronous psycopg connection for the probe.
        import psycopg

        # Convert SQLAlchemy URL to plain psycopg DSN if needed.
        dsn = database_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("readyz: database probe failed: %s", exc)
        return f"database unreachable: {exc}"


def _check_s3(s3_bucket: str | None, endpoint_url: str | None) -> str | None:
    """Return an error string if S3 is unreachable, else None."""
    if not s3_bucket:
        return "S3_BUCKET not configured"
    try:
        kwargs: dict[str, Any] = {}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        s3 = boto3.client("s3", **kwargs)
        s3.head_bucket(Bucket=s3_bucket)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("readyz: S3 probe failed: %s", exc)
        return f"S3 unreachable: {exc}"


def readiness(
    *,
    database_url: str | None,
    s3_bucket: str | None,
    endpoint_url: str | None = None,
) -> JSONResponse:
    """Check that config is loaded and external dependencies are reachable.

    Checks performed:
    - database: a simple ``SELECT 1`` via psycopg
    - S3: ``HeadBucket`` on the configured bucket

    Returns 200 on success, 503 with a reason on any failure.
    """
    db_err = _check_database(database_url)
    if db_err:
        return _fail(db_err)

    s3_err = _check_s3(s3_bucket, endpoint_url)
    if s3_err:
        return _fail(s3_err)

    return _ok()


# ---------------------------------------------------------------------------
# Consumer health listener (container mode only)
# ---------------------------------------------------------------------------


def start_consumer_health_listener(port: int = 8080) -> None:
    """Start a tiny HTTP server in a daemon thread for consumer health checks.

    Serves GET /healthz and GET /readyz with ``{"status":"ok"}``.
    The thread is a daemon so it does not prevent the process from exiting.

    Call this once near the start of ``run_poller`` when running in container
    mode (i.e. when ``LAMBDA_TASK_ROOT`` is not set).
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/healthz", "/readyz"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(  # noqa: A002
            self,
            format: str,
            *args: object,  # noqa: A002
        ) -> None:
            pass  # suppress default access log noise

    bound_port = port

    def _serve() -> None:
        server = HTTPServer(("0.0.0.0", bound_port), _HealthHandler)  # noqa: S104
        logger.info("Consumer health listener started on port %d", bound_port)
        server.serve_forever()

    thread = threading.Thread(target=_serve, daemon=True, name="health-listener")
    thread.start()
