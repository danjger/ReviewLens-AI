"""API read-latency performance test (platform-foundation task 11).

Requirement 7.3: *WHILE a dataset holds up to 1,000 reviews, API reads for the
dataset list and dataset summary SHALL respond in under 500 ms at the 95th
percentile, excluding cold starts and database resume.*

This test seeds a single dataset with 1,000 reviews, then measures p95 request
latency against the **container integration environment** – the API running as
a container (``docker compose up``), reachable at ``PERF_API_BASE_URL`` (default
``http://localhost:8000``), backed by PostgreSQL and LocalStack. It is marked
``@pytest.mark.perf`` and is **not** part of ``make test`` / ``make test-int``;
it runs as its own non-blocking CI job (see ``.github/workflows/ci.yml``).

Endpoints measured (all prefixed ``/api`` per the repo conventions):

- **List** – ``GET /api/datasets`` – the dataset library list.
  *Provided by the ``dataset-library`` spec (its API table). Not implemented in
  this spec.*
- **Summary detail** – ``GET /api/datasets/{id}`` – the full dataset record read
  by the summary page (metadata, ``metrics``, ``status_detail``).
  *Provided by the ``ingestion-summary`` / ``dataset-library`` specs.*
- **Summary reviews** – ``GET /api/datasets/{id}/reviews?page=1&page_size=25`` –
  reads the active version's ``reviews/v{n}.json`` (the 1,000-review file this
  test seeds) from S3 and paginates it. This is the read whose latency the
  1,000-review bound in Req 7.3 is really about.
  *Provided by the ``ingestion-summary`` spec.*

Because those endpoints are built in later specs, this test is written to run
for real once they land and to **skip cleanly** until then:

- skips if the Compose API is not reachable (so it never fails in an
  environment without the stack up), and
- skips if an endpoint responds ``404`` (not implemented yet),

so a green run today means "stack up, endpoints not here yet, measurements
pending"; once the endpoints ship, the same test enforces the p95 bound.

Testing convention: the test seeds its own dataset ID and cleans up both its
database rows and its S3 prefix afterward, leaving nothing behind.
"""

from __future__ import annotations

import json
import os
import statistics
import time
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import httpx
import pytest
from app.core import db as core_db
from app.core.config import get_settings
from app.db.models import Dataset, DatasetStatus, DatasetVersion, SourceType
from app.storage import keys
from sqlalchemy import text

pytestmark = pytest.mark.perf

# ── Tunables (overridable via env for CI / local runs) ───────────────────────

#: Base URL of the API container in the integration environment.
API_BASE_URL = os.environ.get("PERF_API_BASE_URL", "http://localhost:8000").rstrip("/")

#: Number of reviews to seed (the Req 7.3 ceiling).
SEED_REVIEW_COUNT = int(os.environ.get("PERF_SEED_REVIEWS", "1000"))

#: Warmup requests discarded before measuring, to exclude cold starts and the
#: Aurora/database resume that Req 7.3 explicitly excludes.
WARMUP_REQUESTS = int(os.environ.get("PERF_WARMUP", "5"))

#: Measured requests used to compute the percentile.
MEASURE_REQUESTS = int(os.environ.get("PERF_ITERATIONS", "60"))

#: The p95 latency bound from Requirement 7.3, in milliseconds.
P95_BUDGET_MS = float(os.environ.get("PERF_P95_BUDGET_MS", "500"))

#: Per-request timeout. Generous so a slow (but not failing) response is still
#: measured rather than raising.
REQUEST_TIMEOUT_S = 10.0


# ── p95 helper ───────────────────────────────────────────────────────────────


def percentile(samples: list[float], pct: float) -> float:
    """Return the ``pct`` percentile (0–100) of ``samples`` by linear interpolation.

    Pure and unit-testable; used instead of a library call so the method is
    explicit and dependency-free. ``samples`` must be non-empty.
    """
    if not samples:
        raise ValueError("percentile() requires at least one sample")
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


# ── Environment reachability (skip gates) ────────────────────────────────────


def _api_reachable() -> bool:
    """True if the container API answers its health probe."""
    try:
        resp = httpx.get(f"{API_BASE_URL}/healthz", timeout=2.0)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001 - any connection failure means "skip"
        return False


def _database_reachable() -> bool:
    """True if the configured PostgreSQL accepts a connection."""
    try:
        with core_db.get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any connect/config failure means "skip"
        return False


def _schema_ready() -> bool:
    """True if the ``datasets`` table exists (migrations have been applied).

    ``_database_reachable`` only proves PostgreSQL accepts a connection; in CI
    the perf job can reach a running-but-UN-MIGRATED database, where seeding
    would raise ``UndefinedTable`` as a fixture ERROR instead of a clean skip.
    This checks the schema is actually present so the test keeps its
    "skip cleanly until the stack is ready" contract (platform-foundation
    Known Issues: "Perf test errors (not skips) when the DB schema is absent").
    """
    try:
        with core_db.get_engine().connect() as conn:
            exists = conn.execute(text("SELECT to_regclass('public.datasets')")).scalar()
        return exists is not None
    except Exception:  # noqa: BLE001 - any failure means "not ready" → skip
        return False


def _s3_client() -> Any:
    """Return an S3 client pointed at LocalStack when AWS_ENDPOINT_URL is set."""
    settings = get_settings()
    kwargs: dict[str, Any] = {}
    if settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url
    return boto3.client("s3", **kwargs)


# ── Seeding ──────────────────────────────────────────────────────────────────


def _make_reviews(n: int) -> dict[str, Any]:
    """Build the reviews/v1.json payload with ``n`` reviews.

    The shape mirrors what the extraction/analysis specs write: an entity
    profile plus a ``reviews`` list. Review text is synthetic fixture data
    (this test seeds its own data; it never scrapes a site).
    """
    reviews = [
        {
            "id": f"r{i}",
            "author": f"Reviewer {i}",
            "rating": (i % 5) + 1,
            "title": f"Review {i}",
            "body": f"This is synthetic review number {i} used for latency measurement. " * 3,
            "date": "2024-01-01",
            "sentiment": ["negative", "neutral", "positive"][i % 3],
        }
        for i in range(n)
    ]
    return {
        "entity": {"name": "Perf Fixture Product", "platform": "fixtures"},
        "reviews": reviews,
    }


@pytest.fixture()
def seeded_dataset() -> Iterator[str]:
    """Seed one ``updated`` dataset with 1,000 reviews; clean up afterward.

    Creates:

    - a ``Dataset`` row (status ``updated``, ``active_version=1``,
      ``data_version=1``, ``metrics`` populated) so the summary endpoint has
      data to return,
    - a ``DatasetVersion`` row with ``review_count`` = the seed count, and
    - the ``datasets/{id}/reviews/v1.json`` object in S3 with 1,000 reviews.

    Yields the dataset ID. On teardown, deletes the S3 prefix and the rows.
    """
    # Skip gates run BEFORE any seeding so the fixture never touches S3 / the DB
    # unless the full container integration environment is available. This keeps
    # the test a clean skip (not an error) when the stack is not up.
    get_settings.cache_clear()
    core_db.reset_engine()
    if not _api_reachable():
        pytest.skip(
            f"Container API not reachable at {API_BASE_URL}; "
            "run under the container integration environment (make up)"
        )
    if not _database_reachable():
        pytest.skip("PostgreSQL not reachable; run under the container integration environment")
    if not _schema_ready():
        pytest.skip(
            "database schema not migrated (no 'datasets' table); "
            "run `alembic upgrade head` first (the integration env does this via make test-int)"
        )
    bucket = get_settings().s3_bucket
    if not bucket:
        pytest.skip("S3_BUCKET not configured; run under the container integration environment")

    ds_id = str(uuid.uuid4())
    version = 1
    s3 = _s3_client()
    reviews_key = keys.dataset_reviews(ds_id, version)

    # 1) Write the 1,000-review JSON to S3.
    body = json.dumps(_make_reviews(SEED_REVIEW_COUNT)).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=reviews_key, Body=body, ContentType="application/json")

    # 2) Insert the dataset + version rows.
    with core_db.session_scope() as session:
        session.add(
            Dataset(
                id=ds_id,
                name="perf-latency-fixture",
                page_title="Perf Fixture Product",
                source_type=SourceType.URL,
                original_url=f"https://fixtures.example/{ds_id}",
                final_url=f"https://fixtures.example/{ds_id}",
                normalized_url=f"https://fixtures.example/{ds_id}",
                normalized_final_url=f"https://fixtures.example/{ds_id}",
                platform="fixtures",
                status=DatasetStatus.UPDATED,
                status_detail={"events": [], "viability": {"predicted": 1000, "actual": 1000}},
                metrics={
                    "review_count": SEED_REVIEW_COUNT,
                    "average_rating": 3.0,
                    "sentiment": {"positive": 334, "neutral": 333, "negative": 333},
                },
                data_version=version,
                active_version=version,
            )
        )
        session.add(
            DatasetVersion(
                dataset_id=ds_id,
                version=version,
                trigger="perf-seed",
                review_count=SEED_REVIEW_COUNT,
                extraction_method="upload",
                outcome="updated",
            )
        )

    try:
        yield ds_id
    finally:
        # Clean up S3 (the whole dataset prefix) and the rows.
        try:
            prefix = keys.dataset_prefix(ds_id)
            listed = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
            objects = [{"Key": obj["Key"]} for obj in listed.get("Contents", [])]
            if objects:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        with core_db.session_scope() as session:
            # dataset_versions cascades on the FK, but delete explicitly for clarity.
            session.execute(
                text("DELETE FROM dataset_versions WHERE dataset_id = :id"), {"id": ds_id}
            )
            session.execute(text("DELETE FROM datasets WHERE id = :id"), {"id": ds_id})


# ── Measurement ──────────────────────────────────────────────────────────────


def _measure(client: httpx.Client, path: str) -> tuple[list[float], int]:
    """Issue warmup + measured GETs for ``path``; return (latencies_ms, status).

    Warmup requests are discarded (Req 7.3 excludes cold starts and DB resume).
    Returns the latency samples in milliseconds and the status code of the last
    request so the caller can detect a not-yet-implemented endpoint (404).
    """
    status = 0
    for _ in range(WARMUP_REQUESTS):
        resp = client.get(path)
        status = resp.status_code

    latencies: list[float] = []
    for _ in range(MEASURE_REQUESTS):
        start = time.perf_counter()
        resp = client.get(path)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        status = resp.status_code
        latencies.append(elapsed_ms)
    return latencies, status


def _report(report_lines: list[str], label: str, path: str, latencies: list[float]) -> float:
    """Append a human- and CI-readable line for one endpoint; return its p95."""
    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)
    mx = max(latencies)
    line = (
        f"{label} ({path}): "
        f"n={len(latencies)} p50={p50:.1f}ms p95={p95:.1f}ms max={mx:.1f}ms "
        f"budget={P95_BUDGET_MS:.0f}ms"
    )
    report_lines.append(line)
    return p95


def _write_report(report_lines: list[str]) -> None:
    """Print the report and, in CI, append it to the GitHub step summary."""
    header = "## API latency (Req 7.3, 1,000 reviews)"
    print("\n" + header)
    for line in report_lines:
        print("  " + line)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(header + "\n\n")
            for line in report_lines:
                fh.write(f"- {line}\n")
            fh.write("\n")


def test_api_read_latency_p95_under_budget(seeded_dataset: str) -> None:
    """Req 7.3: list and summary reads stay under the p95 budget at 1,000 reviews.

    Skips cleanly when the container API is unreachable or when an endpoint is
    not implemented yet (404), so this is safe to run before the
    ``dataset-library`` / ``ingestion-summary`` specs land, and enforces the
    bound once they do.

    The ``seeded_dataset`` fixture has already verified the container API,
    PostgreSQL, and S3 are reachable (skipping otherwise), so by here the
    environment is up and a dataset of 1,000 reviews is seeded.
    """
    ds_id = seeded_dataset
    targets = [
        ("list", "/api/datasets"),
        ("summary-detail", f"/api/datasets/{ds_id}"),
        ("summary-reviews", f"/api/datasets/{ds_id}/reviews?page=1&page_size=25"),
    ]

    report_lines: list[str] = []
    results: dict[str, float] = {}
    with httpx.Client(base_url=API_BASE_URL, timeout=REQUEST_TIMEOUT_S) as client:
        for label, path in targets:
            latencies, status = _measure(client, path)
            if status == 404:
                report_lines.append(f"{label} ({path}): NOT IMPLEMENTED (404) – skipped")
                continue
            results[label] = _report(report_lines, label, path, latencies)

    _write_report(report_lines)

    if not results:
        pytest.skip(
            "List and summary endpoints are not implemented yet "
            "(provided by the dataset-library and ingestion-summary specs); "
            "latency bound will be enforced once they land"
        )

    over_budget = {label: p95 for label, p95 in results.items() if p95 >= P95_BUDGET_MS}
    assert not over_budget, (
        f"p95 latency exceeded {P95_BUDGET_MS:.0f}ms budget for: "
        + ", ".join(f"{label}={p95:.1f}ms" for label, p95 in over_budget.items())
        + " | full report: "
        + " | ".join(report_lines)
    )


# ── Unit coverage for the pure percentile helper ─────────────────────────────


def test_percentile_basic() -> None:
    """percentile() matches a known distribution and interpolates between points."""
    data = [float(x) for x in range(1, 101)]  # 1..100
    # Linear interpolation over indices 0..99: rank = 0.95 * 99 = 94.05 -> ~95.05.
    assert percentile(data, 95) == pytest.approx(95.05, abs=0.01)
    assert percentile(data, 50) == pytest.approx(50.5, abs=0.01)
    assert percentile([42.0], 95) == 42.0
    assert percentile([10.0, 20.0], 100) == 20.0
    assert percentile([10.0, 20.0], 0) == 10.0


def test_percentile_matches_statistics_quantiles() -> None:
    """Cross-check percentile() against statistics.quantiles on a sample."""
    data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    # statistics.quantiles(method="inclusive") uses the same linear interpolation.
    q = statistics.quantiles(data, n=100, method="inclusive")
    assert percentile(data, 95) == pytest.approx(q[94], abs=1e-9)
