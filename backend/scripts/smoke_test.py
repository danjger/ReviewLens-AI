"""Post-deploy Lambda-mode smoke test for ReviewLens AI.

platform-foundation task 7.2. Design "Testing Strategy": *"A post-deploy smoke
test runs reads and writes through the Data API driver to catch driver
differences."* and *"CI ... run a post-deploy smoke test against the Lambda
deployment."*

This script runs AFTER a deploy, from the GitHub Actions runner, and proves two
things against the LIVE deployment:

1. **HTTP reachability** – the public API answers its health/readiness probes
   through CloudFront (``/healthz`` and ``/readyz``). This exercises the whole
   Lambda-mode HTTP path: CloudFront → API Gateway → the API Lambda.

2. **Data API parity** – a real round-trip READ *and* WRITE through the exact
   same :mod:`app.core.db` engine the application uses, which selects the RDS
   Data API driver in AWS (``settings.is_aws``). Integration tests run on plain
   psycopg against PostgreSQL, so this is the only check that the Data API
   driver itself serialises/deserialises correctly in the real environment.

   To stay independent of the migrated schema (and to leave no residue), the
   write goes to a uniquely-named TEMPORARY table created, written, read back,
   and dropped within one connection. A temporary table is session-scoped, so
   concurrent smoke runs cannot collide.

Configuration comes from the environment, exactly like the running services:

    SMOKE_BASE_URL     Public base URL of the deployed Portal (CloudFront).
                       The script probes ``$SMOKE_BASE_URL/healthz`` and
                       ``/readyz``. When unset, the HTTP checks are skipped
                       (the Data API check still runs).
    DB_RESOURCE_ARN    Aurora cluster ARN   ┐ from the DataStack outputs; set by
    DB_SECRET_ARN      Aurora secret ARN    │ deploy.yml. Presence of both puts
    DB_DATABASE_NAME   logical DB name      ┘ core.db into Data API mode.

Exit code 0 means every enabled check passed; any failure exits non-zero so the
deploy job fails loudly.

Run::

    uv run python scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import uuid

import httpx
from app.core.config import get_settings
from app.core.db import reset_engine, session_scope
from sqlalchemy import text

# Request timeout for the HTTP probes (seconds). Generous to tolerate a cold
# start and an Aurora Serverless v2 resume on the first hit.
HTTP_TIMEOUT_S = 30.0


def check_http(base_url: str) -> None:
    """Probe the deployed API's health and readiness endpoints over HTTPS.

    Raises ``AssertionError`` if either endpoint does not return HTTP 200.
    """
    base = base_url.rstrip("/")
    with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
        for path in ("/healthz", "/readyz"):
            url = f"{base}{path}"
            print(f"[smoke] GET {url}")
            response = client.get(url)
            assert response.status_code == 200, (
                f"{url} returned {response.status_code}, expected 200: {response.text[:200]!r}"
            )
            print(f"[smoke]   ok ({response.status_code})")


def check_data_api_read_write() -> None:
    """Run a READ and a WRITE through the Data API driver and verify the value.

    Uses a uniquely-named TEMPORARY table so the check is schema-independent and
    leaves nothing behind. The table name includes a random suffix so that two
    smoke runs cannot collide even within the same cluster.

    Raises ``AssertionError`` if the written value does not read back exactly,
    which would indicate a Data API driver serialisation difference.
    """
    settings = get_settings()
    assert settings.is_aws, (
        "Data API smoke check requires AWS mode: set DB_RESOURCE_ARN, "
        "DB_SECRET_ARN, and DB_DATABASE_NAME from the stack outputs."
    )

    # Fresh engine so we pick up the environment set by the deploy job.
    reset_engine()

    table = f"smoke_{uuid.uuid4().hex}"
    marker = uuid.uuid4().hex
    print(f"[smoke] Data API read/write via temp table {table}")

    # NOTE on the S608 noqa below: `table` is a server-generated identifier
    # (`smoke_<uuid4-hex>`), never user input, and a table/identifier name
    # cannot be a bound parameter in SQL. The interpolation is safe; data
    # values are still passed as bound parameters.
    with session_scope() as session:
        # WRITE path: create a temporary table and insert a known value.
        session.execute(
            text(f"CREATE TEMPORARY TABLE {table} (id INTEGER PRIMARY KEY, note TEXT)")  # noqa: S608
        )
        session.execute(
            text(f"INSERT INTO {table} (id, note) VALUES (:id, :note)"),  # noqa: S608
            {"id": 1, "note": marker},
        )

        # READ path: read the value back and confirm it round-tripped exactly.
        row = session.execute(
            text(f"SELECT note FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": 1},
        ).one()
        assert row[0] == marker, f"Data API round-trip mismatch: wrote {marker!r}, read {row[0]!r}"

        # Clean up explicitly (the temp table also drops on disconnect).
        session.execute(text(f"DROP TABLE {table}"))  # noqa: S608

    print("[smoke]   ok (read == write)")


def main() -> int:
    """Run every enabled smoke check; return 0 on success, 1 on any failure."""
    failures: list[str] = []

    base_url = os.environ.get("SMOKE_BASE_URL", "").strip()
    if base_url:
        try:
            check_http(base_url)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the runner
            failures.append(f"HTTP health check failed: {exc}")
    else:
        print("[smoke] SMOKE_BASE_URL unset — skipping HTTP health checks")

    try:
        check_data_api_read_write()
    except Exception as exc:  # noqa: BLE001 - report, don't crash the runner
        failures.append(f"Data API read/write check failed: {exc}")

    if failures:
        print("\n[smoke] FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\n[smoke] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
