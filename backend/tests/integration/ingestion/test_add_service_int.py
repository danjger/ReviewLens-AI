"""Integration tests for the Add step end to end (dataset-ingestion task 7.4).

Task 7.1/7.2's unit tests proved :func:`app.ingestion.service.add_items`'s
control flow against fakes; this suite is task 7.4: it drives ``add_items`` and
the ``POST /ingest/checks/{id}/add`` endpoint against the **real** backing
services the design's "Integration tests" bullet calls for —

* the LocalStack ``check-sessions`` DynamoDB table (the Check Session whose
  items carry the stored verdict / existing-dataset match, written through the
  real :mod:`app.ingestion.check_session` store and claimed ``done`` ->
  ``applied`` with a conditional update);
* the LocalStack S3 bucket (the Check capture ``page.html`` / ``plan.json`` /
  ``snapshot.png`` staged under the check prefix, copied into
  ``datasets/{id}/raw/v1`` + ``snapshot/v1`` by ``create_from_check`` and into
  ``raw/v{n}`` by the shared Refresh Service);
* the Compose PostgreSQL database (the ``datasets`` / ``dataset_versions``
  tables, the unique partial index ``uq_datasets_normalized_url``, and the
  Refresh Service's guarded version increment);
* the LocalStack SQS FIFO ``processing-queue.fifo`` (the enqueued
  ``{dataset_id, data_version}`` message with ``MessageGroupId = dataset_id``).

What it proves (Requirements 3.8, 3.9, 6.2, 6.4, 6.5, 6.6, 6.7; Correctness
Properties 5 and 6):

1. **new URL -> v1** — a dataset is created with ``data_version = 1`` and a
   ``dataset_versions`` v1 row (trigger ``initial``), the capture is copied into
   ``raw/v1`` + ``snapshot/v1``, status is ``requested`` with
   ``status_detail.viability`` present, and a processing message is enqueued
   (Requirements 5.1-5.3).
2. **tracked URL -> refresh with no new row** — Add routes an already-tracked
   URL to the Refresh Service: **no** new ``datasets`` row is created, the
   existing dataset gains a v2 with a ``refresh_requested`` event and trigger
   ``duplicate_submission`` (Requirement 6.4).
3. **archived -> restored and refreshed** — a tracked, archived dataset is
   restored (``archived_at`` cleared) and refreshed (Requirement 6.5).
4. **processing -> already refreshing** — adding a tracked URL whose dataset is
   ``processing``/``requested`` makes no new version (Requirement 6.6).
5. **concurrent duplicate -> one dataset** — two concurrent Adds of the same
   *new* normalized URL leave exactly one dataset afterward: the unique index
   lets one INSERT win and the loser falls back to a refresh (Requirement 6.7,
   Property 6).
6. **double-submitted Add -> one dataset** — adding the same item twice (a
   double-clicked Add) creates exactly one dataset: the conditional ``applied``
   marking makes the second Add idempotent.
7. **``wont_work`` refused** — a ``wont_work`` item is refused and creates no
   dataset and no version (Requirement 3.8, Property 5).
8. **``limited`` without confirmation refused** — a ``limited`` item added
   without ``confirm_limited`` is ``needs_confirmation`` and creates nothing
   (Requirement 3.9).

Add itself never calls the AI (the verdict and capture already exist), so no
FakeClaude is installed here; the process-wide offline stub from ``conftest.py``
guarantees no accidental network call anyway.

Running
-------
Run with ``make test-int`` under ``make up`` (needs LocalStack **and** the
Compose PostgreSQL). The module and each test skip cleanly when either is
unavailable, so the suite still *collects* without the stack. Each test uses its
own random check id / dataset ids and cleans up its DynamoDB rows, S3 prefixes,
and database rows, and drains the processing queue.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Iterator

import boto3
import httpx
import pytest
from app.core import db as core_db
from app.core.config import get_settings
from app.db.models import Base, Dataset, DatasetStatus, SourceType
from app.ingestion import check_session, service
from app.ingestion.check_session import CheckItem
from app.ingestion.service import AddRequestItem
from app.storage import keys, s3
from sqlalchemy import text

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"
_CHECK_SESSIONS_TABLE = "check-sessions"
#: The FIFO processing queue provisioned by ``infra/localstack-init``.
_PROCESSING_QUEUE_URL = (
    "http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/processing-queue.fifo"
)


# ---------------------------------------------------------------------------
# Availability probes (skip cleanly without the stack)
# ---------------------------------------------------------------------------


def _localstack_up() -> bool:
    try:
        resp = httpx.get(f"{_ENDPOINT}/_localstack/health", timeout=2.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _database_reachable() -> bool:
    try:
        with core_db.get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any failure means skip
        return False


# ---------------------------------------------------------------------------
# Fixtures: point AWS clients at LocalStack, require the stack, ensure schema
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point AWS clients at LocalStack and reset memoised handles."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", _ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", _S3_BUCKET)
    monkeypatch.setenv("EVENTBRIDGE_BUS_NAME", "reviewlens-events")
    monkeypatch.setenv("PROCESSING_QUEUE_URL", _PROCESSING_QUEUE_URL)
    monkeypatch.setenv("CHECK_SESSIONS_TABLE", _CHECK_SESSIONS_TABLE)
    # The integration suite loads the repo-root ``.env``, which sets
    # ``ORIGIN_VERIFY_SECRET`` — so ``OriginGuardMiddleware`` 403s the headerless
    # ``TestClient`` requests the ``POST /ingest/checks/{id}/add`` endpoint tests
    # make (``test_add_endpoint_new_and_wont_work_mix`` /
    # ``..._expired_session_returns_expired``) before they reach their
    # assertions. Clear the secret so the guard runs in local-dev bypass,
    # matching the library/summary integration suites (dataset-ingestion task
    # 12.3 / Known Issues D).
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", "")
    get_settings.cache_clear()
    s3.reset_client()
    from app.core import queue
    from app.events import publisher

    queue.reset_client()
    publisher.reset_client()
    try:
        yield
    finally:
        get_settings.cache_clear()
        s3.reset_client()
        queue.reset_client()
        publisher.reset_client()


@pytest.fixture(scope="module", autouse=True)
def _require_stack() -> None:
    if not _localstack_up():
        pytest.skip("LocalStack not reachable; run under `make test-int`")


@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    """Ensure the DB schema exists; skip when no PostgreSQL is reachable."""
    get_settings.cache_clear()
    core_db.reset_engine()
    if get_settings().is_aws or not _database_reachable():
        pytest.skip("PostgreSQL not reachable; run under `make test-int` with the stack up")
    engine = core_db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(engine)
    yield
    core_db.reset_engine()


# ---------------------------------------------------------------------------
# Check Session factory (real DynamoDB rows) + capture staging (real S3)
# ---------------------------------------------------------------------------


def _stage_capture(check_id: str, item_id: str, *, with_snapshot: bool = True) -> None:
    """Stage a Check capture (page + plan, optional snapshot) under the prefix.

    Keys all come from :mod:`app.storage.keys`, matching what the real check
    handler would have written, so ``create_from_check`` / the Refresh Service
    copy real objects.
    """
    s3.put_bytes(
        keys.check_page(check_id, item_id),
        b"<html><body><main>captured reviews</main></body></html>",
        content_type="text/html; charset=utf-8",
    )
    s3.put_bytes(
        keys.check_plan(check_id, item_id),
        json.dumps({"method": "selectors", "degraded": False}).encode("utf-8"),
        content_type="application/json",
    )
    if with_snapshot:
        s3.put_bytes(
            keys.check_snapshot(check_id, item_id),
            b"\x89PNG snapshot bytes",
            content_type="image/png",
        )


def _verdict(label: str, *, page_title: str | None = None) -> dict[str, object]:
    """A minimal verdict object of the shape viability.assess writes.

    Carries the label, a couple of reasons, and an evidence block with
    ``page_title`` (used as the dataset name) and verified-review counts, so the
    stored ``status_detail.viability`` is realistic.
    """
    evidence: dict[str, object] = {
        "reviews_verified": 12 if label != "wont_work" else 0,
        "reviews_rejected": 0,
        "method": "selectors",
        "blocker": None if label != "wont_work" else "empty",
        "locator_confidence": "high",
        "main_status": 200,
        "samples": [{"text": "Setup took an afternoon", "rating": 5}],
    }
    if page_title is not None:
        evidence["page_title"] = page_title
    return {
        "verdict": label,
        "reasons": [f"verdict is {label}"],
        "warnings": [],
        "evidence": evidence,
    }


@pytest.fixture()
def check_factory() -> Iterator[Callable[..., tuple[str, str]]]:
    """Create a one-item ``origin="new"`` Check Session with a staged capture.

    Returns ``(check_id, item_id)``. The item is written to DynamoDB in the
    ``done`` state (so the ``done`` -> ``applied`` conditional claim in Add
    succeeds) carrying the given verdict and optional ``existing_dataset`` match.
    Cleans up the DynamoDB rows and the S3 check prefix afterward.
    """
    check_ids: list[str] = []

    def _make(
        *,
        verdict_label: str = "will_work",
        existing_dataset: dict[str, object] | None = None,
        page_title: str | None = "Acme CRM Reviews",
        normalized: str | None = None,
        final_url: str | None = None,
        with_snapshot: bool = True,
        with_capture: bool = True,
    ) -> tuple[str, str]:
        check_id = f"it-{uuid.uuid4().hex}"
        item_id = "u1"
        norm = normalized or f"https://example.com/{check_id}"
        item = CheckItem(
            item_id=item_id,
            input=norm,
            state="done",
            normalized=norm,
            final_url=final_url,
            verdict=_verdict(verdict_label, page_title=page_title),
            existing_dataset=existing_dataset,
        )
        check_session.put_session(check_id, "new", [item])
        if with_capture:
            _stage_capture(check_id, item_id, with_snapshot=with_snapshot)
        check_ids.append(check_id)
        return check_id, item_id

    try:
        yield _make
    finally:
        ddb = boto3.client("dynamodb", region_name=_REGION, endpoint_url=_ENDPOINT)
        client = s3._get_s3_client()
        for cid in check_ids:
            resp = ddb.query(
                TableName=_CHECK_SESSIONS_TABLE,
                KeyConditionExpression="check_id = :c",
                ExpressionAttributeValues={":c": {"S": cid}},
            )
            for row in resp.get("Items", []):
                ddb.delete_item(
                    TableName=_CHECK_SESSIONS_TABLE,
                    Key={"check_id": row["check_id"], "item_id": row["item_id"]},
                )
            listed = client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=f"checks/{cid}/")
            for obj in listed.get("Contents", []):
                key = obj.get("Key")
                if key:
                    client.delete_object(Bucket=_S3_BUCKET, Key=key)


@pytest.fixture()
def existing_dataset_factory() -> Iterator[Callable[..., str]]:
    """Insert a pre-existing URL dataset; clean up its rows + S3 after.

    Returns the dataset id. Used by the tracked / archived / in-flight tests to
    seed a dataset whose ``normalized_url`` matches the checked URL so the
    duplicate lookup and the unique index both see it.
    """
    created: list[str] = []

    def _make(
        *,
        normalized_url: str,
        status: DatasetStatus = DatasetStatus.UPDATED,
        data_version: int = 1,
        archived: bool = False,
        name: str = "Existing Acme CRM",
    ) -> str:
        ds_id = str(uuid.uuid4())
        with core_db.session_scope() as session_db:
            session_db.add(
                Dataset(
                    id=ds_id,
                    name=name,
                    source_type=SourceType.URL,
                    original_url=normalized_url,
                    final_url=normalized_url,
                    normalized_url=normalized_url,
                    normalized_final_url=normalized_url,
                    status=status,
                    status_detail={"events": []},
                    data_version=data_version,
                )
            )
            if archived:
                session_db.execute(
                    text("UPDATE datasets SET archived_at = now() WHERE id = CAST(:id AS uuid)"),
                    {"id": ds_id},
                )
            for v in range(1, data_version + 1):
                session_db.execute(
                    text(
                        "INSERT INTO dataset_versions (dataset_id, version, trigger, requested_at) "
                        "VALUES (CAST(:id AS uuid), :v, 'initial', now())"
                    ),
                    {"id": ds_id, "v": v},
                )
        created.append(ds_id)
        return ds_id

    try:
        yield _make
    finally:
        _cleanup_datasets(created)


def _cleanup_datasets(ids: list[str]) -> None:
    """Delete the given datasets' rows and S3 prefixes (best effort)."""
    if not ids:
        return
    client = s3._get_s3_client()
    with core_db.session_scope() as session_db:
        for ds_id in ids:
            session_db.execute(
                text("DELETE FROM dataset_versions WHERE dataset_id = CAST(:id AS uuid)"),
                {"id": ds_id},
            )
            session_db.execute(
                text("DELETE FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
            )
    for ds_id in ids:
        listed = client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=keys.dataset_prefix(ds_id))
        for obj in listed.get("Contents", []):
            key = obj.get("Key")
            if key:
                client.delete_object(Bucket=_S3_BUCKET, Key=key)


@pytest.fixture()
def track_created() -> Iterator[list[str]]:
    """Collect dataset ids created *by Add during a test* and clean them up.

    The new-URL tests don't know the generated dataset id in advance; they
    append the returned ``AddResult.dataset_id`` here so the fixture removes its
    rows and S3 objects afterward.
    """
    ids: list[str] = []
    try:
        yield ids
    finally:
        _cleanup_datasets(ids)


# ---------------------------------------------------------------------------
# Processing queue helpers
# ---------------------------------------------------------------------------


def _drain_processing_queue() -> list[dict[str, object]]:
    """Receive and delete all messages on the FIFO processing queue, as dicts."""
    sqs = boto3.client("sqs", region_name=_REGION, endpoint_url=_ENDPOINT)
    bodies: list[dict[str, object]] = []
    while True:
        resp = sqs.receive_message(
            QueueUrl=_PROCESSING_QUEUE_URL,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
            VisibilityTimeout=1,
        )
        messages = resp.get("Messages", [])
        if not messages:
            break
        for msg in messages:
            bodies.append(json.loads(msg["Body"]))
            sqs.delete_message(QueueUrl=_PROCESSING_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
    return bodies


@pytest.fixture()
def _clean_queue() -> Iterator[None]:
    """Purge the processing queue before and after so each test sees only its own."""
    _drain_processing_queue()
    yield
    _drain_processing_queue()


# ---------------------------------------------------------------------------
# State readers
# ---------------------------------------------------------------------------


def _dataset_row(ds_id: str) -> tuple[str, int, bool]:
    """Return ``(status, data_version, is_archived)`` from PostgreSQL."""
    with core_db.session_scope() as session_db:
        row = session_db.execute(
            text(
                "SELECT status, data_version, (archived_at IS NOT NULL) "
                "FROM datasets WHERE id = CAST(:id AS uuid)"
            ),
            {"id": ds_id},
        ).one()
    return str(row[0]), int(row[1]), bool(row[2])


def _version_rows(ds_id: str) -> list[tuple[int, str]]:
    with core_db.session_scope() as session_db:
        rows = session_db.execute(
            text(
                "SELECT version, trigger FROM dataset_versions "
                "WHERE dataset_id = CAST(:id AS uuid) ORDER BY version"
            ),
            {"id": ds_id},
        ).all()
    return [(int(v), str(t)) for v, t in rows]


def _status_detail(ds_id: str) -> dict[str, object]:
    with core_db.session_scope() as session_db:
        detail = session_db.execute(
            text("SELECT status_detail FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
        ).scalar_one()
    assert isinstance(detail, dict)
    return detail


def _count_url_datasets(normalized_url: str) -> int:
    with core_db.session_scope() as session_db:
        return int(
            session_db.execute(
                text(
                    "SELECT count(*) FROM datasets "
                    "WHERE source_type = 'url' AND normalized_url = :u"
                ),
                {"u": normalized_url},
            ).scalar_one()
        )


def _item_state(check_id: str, item_id: str) -> str:
    session = check_session.get_session(check_id)
    assert session is not None
    return session.items[item_id].state


def _existing_match(ds_id: str, status: str, *, archived: bool = False) -> dict[str, object]:
    """The ``existing_dataset`` match shape the check handler stores on an item."""
    return {"id": ds_id, "name": "Existing Acme CRM", "archived": archived, "status": status}


# ---------------------------------------------------------------------------
# new URL -> dataset v1 (Requirements 5.1-5.3)
# ---------------------------------------------------------------------------


def test_new_url_creates_dataset_v1(
    check_factory: Callable[..., tuple[str, str]],
    track_created: list[str],
    _clean_queue: None,
) -> None:
    """A new viable URL -> dataset v1, capture copied, requested, enqueued."""
    normalized = f"https://new.example.com/{uuid.uuid4().hex}"
    check_id, item_id = check_factory(verdict_label="will_work", normalized=normalized)

    results = service.add_items(check_id, [AddRequestItem(item_id)])

    assert len(results) == 1
    result = results[0]
    assert result.outcome == "created"
    assert result.dataset_id is not None
    ds_id = result.dataset_id
    track_created.append(ds_id)

    # data_version 1, status requested, viability saved in status_detail.
    status, version, _ = _dataset_row(ds_id)
    assert version == 1
    assert status == DatasetStatus.REQUESTED.value
    detail = _status_detail(ds_id)
    assert "viability" in detail and detail["viability"]["verdict"] == "will_work"  # type: ignore[index]
    assert any(e.get("message") == "requested" for e in detail.get("events", []))  # type: ignore[union-attr]

    # dataset_versions v1 row with trigger `initial`.
    assert _version_rows(ds_id) == [(1, "initial")]

    # Capture copied into raw/v1 + snapshot/v1 (keys from storage.keys).
    assert s3.object_exists(keys.dataset_raw_page(ds_id, 1, 1))
    assert s3.object_exists(keys.dataset_raw_plan(ds_id, 1))
    assert s3.object_exists(keys.dataset_snapshot(ds_id, 1))

    # The Check item is now `applied`.
    assert _item_state(check_id, item_id) == "applied"

    # Exactly one processing message carrying IDs only was enqueued.
    assert _drain_processing_queue() == [{"dataset_id": ds_id, "data_version": 1}]


# ---------------------------------------------------------------------------
# tracked URL -> refresh with NO new dataset row (Requirement 6.4)
# ---------------------------------------------------------------------------


def test_tracked_url_refreshes_without_new_row(
    check_factory: Callable[..., tuple[str, str]],
    existing_dataset_factory: Callable[..., str],
    _clean_queue: None,
) -> None:
    """An already-tracked URL -> refresh (v2), no new datasets row (Req 6.4)."""
    normalized = f"https://tracked.example.com/{uuid.uuid4().hex}"
    existing_id = existing_dataset_factory(
        normalized_url=normalized, status=DatasetStatus.UPDATED, data_version=1
    )
    existing = _existing_match(existing_id, "updated")
    check_id, item_id = check_factory(normalized=normalized, existing_dataset=existing)

    before = _count_url_datasets(normalized)

    results = service.add_items(check_id, [AddRequestItem(item_id)])

    assert results[0].outcome == "refreshed"
    assert results[0].dataset_id == existing_id

    # No new datasets row: still exactly one for this URL, the original.
    assert _count_url_datasets(normalized) == before == 1

    # The original dataset gained v2 with the duplicate_submission trigger and a
    # refresh_requested event; status back to requested.
    status, version, _ = _dataset_row(existing_id)
    assert version == 2
    assert status == DatasetStatus.REQUESTED.value
    assert (2, "duplicate_submission") in _version_rows(existing_id)
    events = _status_detail(existing_id).get("events", [])
    assert any(e.get("message") == "refresh_requested" for e in events)  # type: ignore[union-attr]

    assert _drain_processing_queue() == [{"dataset_id": existing_id, "data_version": 2}]


# ---------------------------------------------------------------------------
# archived tracked URL -> restored and refreshed (Requirement 6.5)
# ---------------------------------------------------------------------------


def test_archived_tracked_url_is_restored_and_refreshed(
    check_factory: Callable[..., tuple[str, str]],
    existing_dataset_factory: Callable[..., str],
    _clean_queue: None,
) -> None:
    """A tracked, archived dataset -> restored (archived_at cleared) + refreshed."""
    normalized = f"https://archived.example.com/{uuid.uuid4().hex}"
    existing_id = existing_dataset_factory(
        normalized_url=normalized,
        status=DatasetStatus.UPDATED,
        data_version=2,
        archived=True,
    )
    existing = _existing_match(existing_id, "updated", archived=True)
    check_id, item_id = check_factory(normalized=normalized, existing_dataset=existing)

    results = service.add_items(check_id, [AddRequestItem(item_id)])

    assert results[0].outcome == "restored_and_refreshed"
    assert results[0].dataset_id == existing_id

    status, version, is_archived = _dataset_row(existing_id)
    assert is_archived is False  # restored
    assert version == 3
    assert status == DatasetStatus.REQUESTED.value
    assert (3, "duplicate_submission") in _version_rows(existing_id)


# ---------------------------------------------------------------------------
# tracked URL while processing -> already refreshing (Requirement 6.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [DatasetStatus.PROCESSING, DatasetStatus.REQUESTED])
def test_tracked_url_in_flight_is_already_refreshing(
    status: DatasetStatus,
    check_factory: Callable[..., tuple[str, str]],
    existing_dataset_factory: Callable[..., str],
    _clean_queue: None,
) -> None:
    """Adding a tracked URL whose dataset is in flight -> no new version (6.6)."""
    normalized = f"https://inflight.example.com/{uuid.uuid4().hex}"
    existing_id = existing_dataset_factory(normalized_url=normalized, status=status, data_version=3)
    existing = _existing_match(existing_id, status.value)
    check_id, item_id = check_factory(normalized=normalized, existing_dataset=existing)

    results = service.add_items(check_id, [AddRequestItem(item_id)])

    assert results[0].outcome == "already_refreshing"
    # No version bump, no new row, nothing enqueued.
    _, version, _ = _dataset_row(existing_id)
    assert version == 3
    assert _version_rows(existing_id) == [(1, "initial"), (2, "initial"), (3, "initial")]
    assert _drain_processing_queue() == []


# ---------------------------------------------------------------------------
# concurrent duplicate of a NEW URL -> exactly one dataset (Property 6, 6.7)
# ---------------------------------------------------------------------------


def test_concurrent_duplicate_new_url_yields_one_dataset(
    check_factory: Callable[..., tuple[str, str]],
    track_created: list[str],
    _clean_queue: None,
) -> None:
    """Two concurrent Adds of the same NEW URL -> exactly one dataset (Property 6).

    Both checks see "not tracked" (no existing dataset), so both Adds try to
    INSERT. The partial unique index ``uq_datasets_normalized_url`` lets exactly
    one INSERT win; the loser catches the violation, cleans up its partial
    objects, and falls back to refreshing the winner — so afterward exactly one
    dataset exists for the URL (Requirement 6.7).

    _Validates: Requirements 6.2, 6.4, 6.7 (design Correctness Property 6)._
    """
    normalized = f"https://race.example.com/{uuid.uuid4().hex}"
    # Two independent Check Sessions for the SAME normalized URL.
    check_a, item_a = check_factory(normalized=normalized)
    check_b, item_b = check_factory(normalized=normalized)

    results: list[service.AddResult] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _worker(cid: str, iid: str) -> None:
        try:
            barrier.wait(timeout=10)
            results.extend(service.add_items(cid, [AddRequestItem(iid)]))
        except BaseException as exc:  # noqa: BLE001 - carried to the asserting thread
            errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=(check_a, item_a)),
        threading.Thread(target=_worker, args=(check_b, item_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"an Add thread raised: {errors}"

    # Exactly one dataset exists for this normalized URL.
    assert _count_url_datasets(normalized) == 1

    # Track whatever dataset(s) got created for cleanup (both results carry the
    # single winner's id — one `created`, one `refreshed`/`already_refreshing`).
    created_ids = {r.dataset_id for r in results if r.dataset_id}
    track_created.extend(created_ids)
    assert len(created_ids) == 1

    outcomes = [r.outcome for r in results]
    # Exactly one winner `created`; the loser hit the unique-URL violation and
    # fell back to refreshing the winner. Because the winner's INSERT already
    # births the dataset `requested`, the loser's refresh guard sees an
    # in-flight status and reports `already_refreshing` (it reports `refreshed`
    # only if it somehow got in before the winner was marked in-flight). Assert
    # on the multiset, not sort order: `already_refreshing` sorts before
    # `created`, so an index-based check would be fooled.
    assert outcomes.count("created") == 1
    other = [o for o in outcomes if o != "created"]
    assert other == [o for o in other if o in {"refreshed", "already_refreshing"}]
    assert len(other) == 1


# ---------------------------------------------------------------------------
# double-submitted Add (same item twice) -> one dataset
# ---------------------------------------------------------------------------


def test_double_submitted_add_creates_one_dataset(
    check_factory: Callable[..., tuple[str, str]],
    track_created: list[str],
    _clean_queue: None,
) -> None:
    """Adding the same item twice creates exactly one dataset (idempotent Add).

    The conditional ``done`` -> ``applied`` claim means the first Add creates the
    dataset and the second finds the item already ``applied`` and returns the
    idempotent ``created`` without creating a second dataset.
    """
    normalized = f"https://double.example.com/{uuid.uuid4().hex}"
    check_id, item_id = check_factory(normalized=normalized)

    first = service.add_items(check_id, [AddRequestItem(item_id)])
    assert first[0].outcome == "created"
    ds_id = first[0].dataset_id
    assert ds_id is not None
    track_created.append(ds_id)

    # Second submission of the SAME item.
    second = service.add_items(check_id, [AddRequestItem(item_id)])
    assert second[0].outcome == "created"  # idempotent re-report
    assert second[0].dataset_id is None  # the reapplied path doesn't re-create

    # Exactly one dataset for this URL, with a single v1 row.
    assert _count_url_datasets(normalized) == 1
    assert _version_rows(ds_id) == [(1, "initial")]

    # Only one processing message was enqueued (from the first Add).
    assert _drain_processing_queue() == [{"dataset_id": ds_id, "data_version": 1}]


# ---------------------------------------------------------------------------
# wont_work refused -> no dataset, no version (Requirement 3.8, Property 5)
# ---------------------------------------------------------------------------


def test_wont_work_item_is_refused_and_creates_nothing(
    check_factory: Callable[..., tuple[str, str]],
    _clean_queue: None,
) -> None:
    """A `wont_work` item is refused; no dataset or version is created (3.8).

    _Validates: Requirement 3.8 (design Correctness Property 5)._
    """
    normalized = f"https://wontwork.example.com/{uuid.uuid4().hex}"
    check_id, item_id = check_factory(verdict_label="wont_work", normalized=normalized)

    results = service.add_items(check_id, [AddRequestItem(item_id)])

    assert results[0].outcome == "refused_wont_work"
    assert results[0].dataset_id is None
    # No dataset created for the URL, nothing enqueued, item not applied.
    assert _count_url_datasets(normalized) == 0
    assert _drain_processing_queue() == []
    assert _item_state(check_id, item_id) == "done"  # never claimed/applied


# ---------------------------------------------------------------------------
# limited without confirmation refused (Requirement 3.9)
# ---------------------------------------------------------------------------


def test_limited_without_confirmation_is_refused(
    check_factory: Callable[..., tuple[str, str]],
    _clean_queue: None,
) -> None:
    """A `limited` item added without confirm_limited -> needs_confirmation (3.9)."""
    normalized = f"https://limited.example.com/{uuid.uuid4().hex}"
    check_id, item_id = check_factory(verdict_label="limited", normalized=normalized)

    results = service.add_items(check_id, [AddRequestItem(item_id, confirm_limited=False)])

    assert results[0].outcome == "needs_confirmation"
    assert results[0].dataset_id is None
    assert _count_url_datasets(normalized) == 0
    assert _drain_processing_queue() == []
    assert _item_state(check_id, item_id) == "done"  # nothing created, not applied


def test_limited_with_confirmation_creates_dataset(
    check_factory: Callable[..., tuple[str, str]],
    track_created: list[str],
    _clean_queue: None,
) -> None:
    """A `limited` item WITH confirm_limited is added as a new dataset (3.9)."""
    normalized = f"https://limitedok.example.com/{uuid.uuid4().hex}"
    check_id, item_id = check_factory(verdict_label="limited", normalized=normalized)

    results = service.add_items(check_id, [AddRequestItem(item_id, confirm_limited=True)])

    assert results[0].outcome == "created"
    ds_id = results[0].dataset_id
    assert ds_id is not None
    track_created.append(ds_id)
    assert _count_url_datasets(normalized) == 1
    assert _drain_processing_queue() == [{"dataset_id": ds_id, "data_version": 1}]


# ---------------------------------------------------------------------------
# Through the real POST /ingest/checks/{id}/add endpoint
# ---------------------------------------------------------------------------


def test_add_endpoint_new_and_wont_work_mix(
    check_factory: Callable[..., tuple[str, str]],
    track_created: list[str],
    _clean_queue: None,
) -> None:
    """The Add endpoint returns per-item outcomes: created + refused_wont_work.

    Drives the whole stack through ``POST /ingest/checks/{id}/add`` (task 7.3)
    so the controller, service, DynamoDB claim, S3 copy, DB insert, and enqueue
    all run together.
    """
    from app.api import app
    from fastapi.testclient import TestClient

    normalized = f"https://endpoint.example.com/{uuid.uuid4().hex}"
    # Two items in one session: one will_work (created) and one wont_work.
    check_id = f"it-{uuid.uuid4().hex}"
    good = CheckItem(
        item_id="u1",
        input=normalized,
        state="done",
        normalized=normalized,
        verdict=_verdict("will_work", page_title="Endpoint Acme"),
    )
    bad = CheckItem(
        item_id="u2",
        input=f"{normalized}/bad",
        state="done",
        normalized=f"{normalized}/bad",
        verdict=_verdict("wont_work"),
    )
    check_session.put_session(check_id, "new", [good, bad])
    _stage_capture(check_id, "u1")

    try:
        client = TestClient(app)
        resp = client.post(
            f"/api/ingest/checks/{check_id}/add",
            json={"items": [{"item_id": "u1"}, {"item_id": "u2"}]},
        )
        assert resp.status_code == 200
        results = {r["item_id"]: r for r in resp.json()["results"]}

        assert results["u1"]["outcome"] == "created"
        assert results["u1"]["dataset_id"] is not None
        track_created.append(results["u1"]["dataset_id"])
        assert results["u2"]["outcome"] == "refused_wont_work"
        assert results["u2"]["dataset_id"] is None

        assert _count_url_datasets(normalized) == 1
        bodies = _drain_processing_queue()
        assert bodies == [{"dataset_id": results["u1"]["dataset_id"], "data_version": 1}]
    finally:
        ddb = boto3.client("dynamodb", region_name=_REGION, endpoint_url=_ENDPOINT)
        resp = ddb.query(
            TableName=_CHECK_SESSIONS_TABLE,
            KeyConditionExpression="check_id = :c",
            ExpressionAttributeValues={":c": {"S": check_id}},
        )
        for row in resp.get("Items", []):
            ddb.delete_item(
                TableName=_CHECK_SESSIONS_TABLE,
                Key={"check_id": row["check_id"], "item_id": row["item_id"]},
            )
        client_s3 = s3._get_s3_client()
        listed = client_s3.list_objects_v2(Bucket=_S3_BUCKET, Prefix=f"checks/{check_id}/")
        for obj in listed.get("Contents", []):
            key = obj.get("Key")
            if key:
                client_s3.delete_object(Bucket=_S3_BUCKET, Key=key)


def test_add_endpoint_expired_session_returns_expired(_clean_queue: None) -> None:
    """Add against an absent/expired session -> outcome `expired`, 200 (Req 5.4)."""
    from app.api import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    missing = f"it-missing-{uuid.uuid4().hex}"
    resp = client.post(
        f"/api/ingest/checks/{missing}/add",
        json={"items": [{"item_id": "u1"}]},
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results == [{"item_id": "u1", "outcome": "expired", "dataset_id": None, "message": None}]
