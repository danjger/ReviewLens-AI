"""Scale test: exactly-once-in-effect under concurrent writers.

Covers platform-foundation **task 9** and Requirements 3.1 (stateless, shared
state external), 3.5 (idempotent handlers tolerate duplicate/concurrent
delivery), 3.6 (scheduled jobs safe on more than one instance), and 8.8 (the CI
scale test: two instances of each consumer, every message processed exactly
once in effect — no duplicate datasets, versions, check results, or Exchanges).

What this file asserts **today**
--------------------------------
The platform's queue handlers and sweeper are still stubs in this spec (they are
implemented in ``review-extraction``, ``dataset-ingestion``, ``review-analysis``
and ``dataset-library``). A full check→processing→exchange scale run therefore
can't drive real work yet. What *does* exist is the set of concurrency and
idempotency **primitives** the design relies on, so this harness proves
"exactly once in effect" at the data layer, under genuinely concurrent writers,
against the Compose stack (PostgreSQL + LocalStack):

- **Dataset uniqueness** — N concurrent inserts of the *same* ``normalized_url``
  (``source_type='url'``) collapse to exactly one row via the unique partial
  index (the "races on creation" rule; Requirement 3.1 / 8.8 "no duplicate
  datasets").
- **Status history append-only under concurrency** — many concurrent
  ``transition()`` / ``log_event()`` calls on one dataset keep
  ``status_detail.events`` lossless and in time order, and the last status event
  equals the ``status`` column (Correctness Property 5). This is the data-layer
  face of "no overlapping processing corrupts one dataset" (Requirement 3.5).
- **DynamoDB conditional-claim idempotency** — two concurrent workers racing the
  check-item claim (``state`` pending→checking via a conditional update): exactly
  one wins, the rest no-op. This is how "no duplicate check results" holds across
  instances (Requirement 3.5, design "Horizontal-scaling rules").
- **Sweeper safety** — two concurrent sweeper transactions over a shared set of
  rows using ``SELECT ... FOR UPDATE SKIP LOCKED``: no row is claimed by both
  (Requirement 3.6).

The suite asserts on **final database / DynamoDB state, not on timing**
(testing convention). Each test creates its own dataset IDs, DynamoDB keys, and
table, and cleans them up.

What is deferred (see the skipped test at the bottom)
-----------------------------------------------------
The full consumer-level assertion — start two instances of each queue consumer
and two sweeper runs, drive a real check→add→process→exchange flow, and assert
no duplicate datasets / versions / check results / Exchanges and no overlapping
FIFO processing of one dataset — depends on handlers this spec does not provide.
It is present as a clearly-marked, skipped placeholder documenting exactly what
it will assert and which spec/task provides each piece, so the harness is in
place and the gap is explicit rather than silently missing.

Running two instances under ``make test-scale``
-----------------------------------------------
``make test-scale`` runs ``docker compose up -d --wait`` then
``pytest tests/scale -v -m scale``. The Compose file defines a single instance
of each consumer. To exercise two instances of each consumer (Requirement 8.8),
bring the stack up with ``--scale`` before running the suite::

    docker compose up -d --wait \\
        --scale workers-check=2 \\
        --scale workers-processing=2 \\
        --scale push-consumer=2

(The ``sweeper`` service already loops; a second concurrent sweep is exercised
directly in :func:`test_sweeper_skip_locked_never_double_claims`.) A recommended
follow-up is to add those ``--scale`` flags to the ``test-scale`` Makefile target
once the handlers from the later specs land; it is intentionally **not** changed
here so the existing target keeps working while the handlers are stubs.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

import boto3
import pytest
from app.core import db as core_db
from app.core.config import get_settings
from app.db import status as status_mod
from app.db.models import Base, Dataset, DatasetStatus, SourceType
from botocore.exceptions import ClientError
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

pytestmark = pytest.mark.scale


# ---------------------------------------------------------------------------
# Reachability guards (skip cleanly when the Compose stack is not up)
# ---------------------------------------------------------------------------


def _database_reachable() -> bool:
    """Return True if the configured PostgreSQL accepts a connection."""
    try:
        with core_db.get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any connect/config failure means "skip"
        return False


def _localstack_reachable() -> bool:
    """Return True if LocalStack DynamoDB is reachable via the configured endpoint."""
    settings = get_settings()
    if not settings.aws_endpoint_url:
        return False
    try:
        client = boto3.client("dynamodb", endpoint_url=settings.aws_endpoint_url)
        client.list_tables()
        return True
    except Exception:  # noqa: BLE001 - any connect failure means "skip"
        return False


@pytest.fixture(scope="module", autouse=True)
def _stack() -> Iterator[None]:
    """Prepare the live schema for the module; skip when the stack is down.

    Mirrors the integration suite: refresh settings, reset the engine, and skip
    the whole module when PostgreSQL (or Data API mode) makes the local stack
    unavailable. The schema is created once and dropped afterwards.
    """
    get_settings.cache_clear()
    core_db.reset_engine()
    if get_settings().is_aws or not _database_reachable():
        pytest.skip("PostgreSQL not reachable; run under `make test-scale` with the stack up")

    engine = core_db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(engine)
    try:
        yield
    finally:
        Base.metadata.drop_all(engine)
        core_db.reset_engine()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dynamodb_client() -> DynamoDBClient:
    """Return a DynamoDB client bound to the LocalStack endpoint."""
    settings = get_settings()
    return cast(
        "DynamoDBClient",
        boto3.client("dynamodb", endpoint_url=settings.aws_endpoint_url),
    )


def _events(ds_id: str) -> list[dict[str, Any]]:
    """Read the ``status_detail.events`` array for a dataset."""
    with core_db.session_scope() as session:
        detail = session.execute(
            text("SELECT status_detail FROM datasets WHERE id = :id"), {"id": ds_id}
        ).scalar_one()
    assert isinstance(detail, dict)
    events = detail["events"]
    assert isinstance(events, list)
    return events


def _run_concurrently(fn: Any, args_list: list[Any], max_workers: int) -> list[Any]:
    """Run ``fn(arg)`` for every arg across a thread pool and collect results.

    Each worker captures its own result or exception so the caller can assert on
    the *final* set of outcomes rather than on timing or ordering.
    """
    results: list[Any] = []
    lock = threading.Lock()

    def _wrapped(arg: Any) -> None:
        try:
            value = fn(arg)
            outcome: tuple[str, Any] = ("ok", value)
        except Exception as exc:  # noqa: BLE001 - we classify outcomes below
            outcome = ("error", exc)
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_wrapped, args_list))
    return results


# ---------------------------------------------------------------------------
# 1. Dataset uniqueness under concurrent creation (no duplicate datasets)
# ---------------------------------------------------------------------------


def test_concurrent_same_url_inserts_yield_one_dataset() -> None:
    """Concurrent inserts of one normalized URL collapse to a single row.

    Requirements 3.1 / 8.8 ("no duplicate datasets"): the unique partial index
    on ``normalized_url`` where ``source_type = 'url'`` is the race-on-creation
    guard. Firing N concurrent inserts of the same URL must leave exactly one
    dataset row; the losers raise ``IntegrityError`` and roll back.
    """
    normalized = f"https://example.com/scale-{uuid.uuid4()}"
    attempts = 8

    def _insert(_: int) -> None:
        with core_db.session_scope() as session:
            session.add(
                Dataset(
                    id=str(uuid.uuid4()),
                    name="scale-uniqueness",
                    source_type=SourceType.URL,
                    original_url=normalized,
                    normalized_url=normalized,
                    status=DatasetStatus.REQUESTED,
                    status_detail={"events": []},
                )
            )

    try:
        results = _run_concurrently(_insert, list(range(attempts)), max_workers=attempts)

        # Exactly one writer committed; the rest lost the unique-index race.
        with core_db.session_scope() as session:
            count = session.execute(
                text(
                    "SELECT count(*) FROM datasets "
                    "WHERE normalized_url = :u AND source_type = 'url'"
                ),
                {"u": normalized},
            ).scalar_one()
        assert count == 1, f"expected exactly one dataset for {normalized}, found {count}"

        oks = [r for r in results if r[0] == "ok"]
        errors = [r for r in results if r[0] == "error"]
        assert len(oks) == 1
        # Every loser failed specifically on the uniqueness constraint.
        assert len(errors) == attempts - 1
        assert all(isinstance(e[1], IntegrityError) for e in errors)
    finally:
        with core_db.session_scope() as session:
            session.execute(
                text("DELETE FROM datasets WHERE normalized_url = :u"),
                {"u": normalized},
            )


# ---------------------------------------------------------------------------
# 2. Status history append-only under concurrency (Property 5)
# ---------------------------------------------------------------------------


@pytest.fixture()
def dataset_id() -> Iterator[str]:
    """Insert a fresh ``requested`` URL dataset; delete it after the test."""
    ds_id = str(uuid.uuid4())
    with core_db.session_scope() as session:
        session.add(
            Dataset(
                id=ds_id,
                name="scale-status",
                source_type=SourceType.URL,
                original_url=f"https://example.com/{ds_id}",
                normalized_url=f"https://example.com/{ds_id}",
                status=DatasetStatus.REQUESTED,
                status_detail={"events": []},
            )
        )
    try:
        yield ds_id
    finally:
        with core_db.session_scope() as session:
            session.execute(text("DELETE FROM datasets WHERE id = :id"), {"id": ds_id})


@pytest.fixture()
def _no_eventbridge() -> Iterator[None]:
    """Patch out the EventBridge publish so this test focuses on the database."""
    from unittest.mock import patch

    with patch.object(status_mod, "publish_event"):
        yield


def test_concurrent_status_writes_are_lossless_and_ordered(
    dataset_id: str, _no_eventbridge: None
) -> None:
    """Property 5 under concurrency: no event lost, ordered, status consistent.

    Many concurrent ``log_event`` writers plus a terminal ``transition`` on one
    dataset must leave ``status_detail.events`` with every appended event
    present and in non-decreasing time order, and the last *status* event equal
    to the ``status`` column. This is the data-layer guarantee behind "two
    instances never corrupt one dataset" (Requirement 3.5).
    """
    writers = 20

    def _append(n: int) -> None:
        status_mod.log_event(dataset_id, f"progress-{n}", extra={"n": n})

    _run_concurrently(_append, list(range(writers)), max_workers=writers)
    # A terminal transition after the concurrent burst sets the status column.
    status_mod.transition(dataset_id, DatasetStatus.UPDATED, "done")

    events = _events(dataset_id)

    # No progress event was lost under concurrency.
    progress = {str(e["message"]) for e in events if e["status"] is None}
    assert progress == {f"progress-{n}" for n in range(writers)}

    # Events are in non-decreasing time order (append-only, no reordering).
    times = [datetime.fromisoformat(str(e["at"])) for e in events]
    assert times == sorted(times)

    # The last status event equals the status column (Property 5).
    status_events = [e for e in events if e["status"] is not None]
    assert status_events[-1]["status"] == "updated"
    with core_db.session_scope() as session:
        col_status = session.execute(
            text("SELECT status FROM datasets WHERE id = :id"), {"id": dataset_id}
        ).scalar_one()
    assert col_status == "updated"


# ---------------------------------------------------------------------------
# 3. DynamoDB conditional-claim idempotency (no duplicate check results)
# ---------------------------------------------------------------------------


@pytest.fixture()
def check_items_table() -> Iterator[str]:
    """Create a throwaway DynamoDB table modelling check-items; delete it after.

    The real ``check-sessions`` table is keyed by ``check_id`` only; a check
    *item* needs a composite key, and this test must not disturb shared data.
    So it provisions its own table with ``(check_id, item_id)`` and tears it
    down, honouring the "create your own keys and clean up" convention.
    """
    if not _localstack_reachable():
        pytest.skip("LocalStack DynamoDB not reachable; run under `make test-scale`")

    client = _dynamodb_client()
    table_name = f"scale-check-items-{uuid.uuid4().hex[:12]}"
    client.create_table(
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "check_id", "AttributeType": "S"},
            {"AttributeName": "item_id", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "check_id", "KeyType": "HASH"},
            {"AttributeName": "item_id", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=table_name)
    try:
        yield table_name
    finally:
        try:
            client.delete_table(TableName=table_name)
        except ClientError:  # pragma: no cover - best-effort cleanup
            pass


def test_conditional_claim_lets_exactly_one_worker_win(check_items_table: str) -> None:
    """Only one of several concurrent workers claims a pending check item.

    This is the "check items use a conditional DynamoDB update
    (``state = pending → checking``) so only one instance works on an item"
    rule from the design. Several instances racing the same item must produce
    exactly one successful claim; the rest fail the condition and no-op — the
    guarantee behind "no duplicate check results" (Requirement 3.5).
    """
    client = _dynamodb_client()
    check_id = str(uuid.uuid4())
    item_id = "u1"
    workers = 6

    # Seed the item in the `pending` state.
    client.put_item(
        TableName=check_items_table,
        Item={
            "check_id": {"S": check_id},
            "item_id": {"S": item_id},
            "state": {"S": "pending"},
        },
    )

    def _claim(worker: int) -> bool:
        """Attempt the pending→checking claim; return True iff this worker won."""
        try:
            client.update_item(
                TableName=check_items_table,
                Key={"check_id": {"S": check_id}, "item_id": {"S": item_id}},
                UpdateExpression="SET #s = :checking, worker = :w",
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={
                    ":checking": {"S": "checking"},
                    ":pending": {"S": "pending"},
                    ":w": {"N": str(worker)},
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    results = _run_concurrently(_claim, list(range(workers)), max_workers=workers)

    wins = [r for r in results if r[0] == "ok" and r[1] is True]
    losses = [r for r in results if r[0] == "ok" and r[1] is False]
    assert len(wins) == 1, "exactly one worker must win the conditional claim"
    assert len(losses) == workers - 1

    # Final state reflects the single winner.
    item = client.get_item(
        TableName=check_items_table,
        Key={"check_id": {"S": check_id}, "item_id": {"S": item_id}},
    )["Item"]
    assert item["state"]["S"] == "checking"


# ---------------------------------------------------------------------------
# 4. Sweeper safety: SELECT ... FOR UPDATE SKIP LOCKED (Requirement 3.6)
# ---------------------------------------------------------------------------


def test_sweeper_skip_locked_never_double_claims(_no_eventbridge: None) -> None:
    """Two concurrent sweeps never claim the same row (``SKIP LOCKED``).

    The sweeper claims rows with ``SELECT ... FOR UPDATE SKIP LOCKED`` so that
    several sweeper instances running at once never act on the same dataset
    (Requirement 3.6). This test seeds a set of ``requested`` rows, runs two
    concurrent "sweep" transactions that each claim and mark rows, and asserts
    the two claim sets are disjoint and together cover every row exactly once.
    """
    seeded = 12
    ids = [str(uuid.uuid4()) for _ in range(seeded)]
    with core_db.session_scope() as session:
        for ds_id in ids:
            session.add(
                Dataset(
                    id=ds_id,
                    name="scale-sweep",
                    source_type=SourceType.URL,
                    original_url=f"https://example.com/sweep-{ds_id}",
                    normalized_url=f"https://example.com/sweep-{ds_id}",
                    status=DatasetStatus.REQUESTED,
                    status_detail={"events": []},
                )
            )

    id_set = set(ids)
    barrier = threading.Barrier(2)

    def _sweep(marker: str) -> list[str]:
        """Claim all claimable seeded rows in one transaction, marking each.

        Uses the engine directly (one connection = one transaction) so the row
        locks held by ``FOR UPDATE`` persist for the whole claim/update, exactly
        as a real sweeper transaction would. ``SKIP LOCKED`` makes the two
        sweeps step over each other's locked rows instead of blocking.
        """
        engine = core_db.get_engine()
        claimed: list[str] = []
        with engine.begin() as conn:
            # Line both sweeps up so they contend for the same rows at once.
            barrier.wait(timeout=30)
            rows = conn.execute(
                text(
                    "SELECT id FROM datasets "
                    "WHERE status = 'requested' AND id = ANY(:ids) "
                    "FOR UPDATE SKIP LOCKED"
                ).bindparams(bindparam("ids", value=ids, expanding=False)),
            ).fetchall()
            for (row_id,) in rows:
                conn.execute(
                    text(
                        "UPDATE datasets SET status = 'processing', name = :marker WHERE id = :id"
                    ),
                    {"marker": marker, "id": row_id},
                )
                claimed.append(str(row_id))
        return claimed

    try:
        results = _run_concurrently(_sweep, ["sweep-a", "sweep-b"], max_workers=2)
        claim_lists = [r[1] for r in results if r[0] == "ok"]
        assert len(claim_lists) == 2, [r for r in results if r[0] == "error"]
        set_a, set_b = set(claim_lists[0]), set(claim_lists[1])

        # No row claimed by both sweeps (SKIP LOCKED kept them disjoint).
        assert set_a.isdisjoint(set_b), "a row was claimed by both sweeps"
        # Together the two sweeps claimed every seeded row exactly once.
        assert set_a | set_b == id_set

        # Every seeded row ended up processing, none left requested.
        with core_db.session_scope() as session:
            remaining = session.execute(
                text(
                    "SELECT count(*) FROM datasets WHERE id = ANY(:ids) AND status = 'requested'"
                ).bindparams(bindparam("ids", value=ids, expanding=False)),
            ).scalar_one()
        assert remaining == 0
    finally:
        with core_db.session_scope() as session:
            session.execute(
                text("DELETE FROM datasets WHERE id = ANY(:ids)").bindparams(
                    bindparam("ids", value=ids, expanding=False)
                ),
            )


# ---------------------------------------------------------------------------
# 5. Deferred: full consumer-level scale run (handlers from later specs)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Requires real queue handlers and the sweeper, which are stubs in "
        "platform-foundation. The check handler lands in review-extraction "
        "(task 7) and dataset-ingestion; the processing/analysis handler in "
        "review-analysis; the push handler in dataset-library; and the sweeper "
        "in review-analysis. Enable once those exist and run the stack with "
        "`docker compose up -d --wait --scale workers-check=2 "
        "--scale workers-processing=2 --scale push-consumer=2`."
    )
)
def test_two_instances_each_consumer_process_exactly_once() -> None:
    """Full exactly-once scale run across two instances of every consumer.

    When the handlers from the later specs exist, this test will, against the
    Compose stack brought up with two instances of each consumer:

    1. Enqueue the same check items twice onto ``check-queue`` and assert each
       check item produces exactly one verdict / check result (no duplicate
       check results), even though two ``workers-check`` instances consume.
    2. Add the same viable URL concurrently and assert exactly one dataset and
       one ``dataset_versions`` row are created (no duplicate datasets or
       versions), relying on the unique index proven above.
    3. Enqueue two FIFO ``processing-queue`` messages for the same dataset
       (same message group = dataset ID) and assert, from the ordered
       ``status_detail.events``, that processing of one dataset never overlaps
       across the two ``workers-processing`` instances.
    4. Publish duplicate ``dataset.status.changed`` events and assert the two
       ``push-consumer`` instances deliver one WebSocket push per connection
       (no duplicate Exchanges).
    5. Run two sweeper passes concurrently and assert no dataset is re-enqueued
       or marked twice (the ``SKIP LOCKED`` guarantee proven above, end to end).

    The assertions are on final database / DynamoDB / S3 state, never on timing
    (testing convention).
    """
    raise AssertionError("placeholder: see docstring and skip reason")
