"""Integration tests for upload ingestion end to end (dataset-ingestion 8.3).

Task 8.2's unit tests proved the parser against bytes, and task 8.3's route
tests proved the controllers against fakes; this suite drives
:func:`app.ingestion.service.create_from_upload` against the **real** backing
services the design's "Integration tests" bullet calls for ("Uploads:
pre-signed flow, preview over the row limit, invalid file deleted"):

* the LocalStack S3 bucket — the staged ``uploads/{upload_id}/file`` object
  (written as the pre-signed PUT would have), copied into
  ``datasets/{id}/raw/v1/upload.csv`` and the generated
  ``datasets/{id}/raw/v1/mapping.json`` (keys from :mod:`app.storage.keys`);
* the Compose PostgreSQL database — the ``datasets`` row (``source_type =
  upload``, status ``requested``) and its v1 ``dataset_versions`` row;
* the LocalStack SQS FIFO ``processing-queue.fifo`` — the enqueued
  ``{dataset_id, data_version}`` message with ``MessageGroupId = dataset_id``.

What it proves (Requirements 7.3, 7.4):

1. **valid upload → v1 dataset** — the file is copied to ``raw/v1/upload.csv``,
   ``mapping.json`` (mapping + keep rule) is written, a ``datasets`` row is
   created with ``source_type = upload``, status ``requested``, URL columns
   null, a v1 ``dataset_versions`` row (trigger ``initial``), and one processing
   message is enqueued (Requirement 7.4).
2. **over-limit upload → preview keep rule** — a file with more usable rows than
   ``MAX_REVIEWS`` records ``will_keep = MAX_REVIEWS`` with the date keep rule
   in ``mapping.json`` (Requirement 7.6).
3. **invalid upload → deleted, nothing created** — a file with no usable text
   column raises ``UploadInvalidError``, the staged object is deleted, and no
   dataset row or object is left behind (Requirement 7.3).

Upload ingestion never calls the AI, so no FakeClaude is installed here; the
process-wide offline stub from ``conftest.py`` guards against any accidental
network call anyway.

Running
-------
Run with ``make test-int`` under ``make up`` (needs LocalStack **and** the
Compose PostgreSQL). The module and each test skip cleanly when either is
unavailable, so the suite still *collects* without the stack. Each test uses its
own random upload id and cleans up its staged object, the created dataset's
rows and S3 prefix, and drains the processing queue.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import boto3
import httpx
import pytest
from app.core import db as core_db
from app.core.config import get_settings
from app.db.models import Base, DatasetStatus, SourceType
from app.ingestion import service
from app.ingestion.upload_parser import UploadInvalidError
from app.storage import keys, s3
from sqlalchemy import text

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"
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
    get_settings.cache_clear()
    s3.reset_client()
    from app.core import queue

    queue.reset_client()
    try:
        yield
    finally:
        get_settings.cache_clear()
        s3.reset_client()
        queue.reset_client()


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
# Upload staging + cleanup helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def upload_factory() -> Iterator[list[str]]:
    """Collect staged upload ids and remove their S3 objects afterward."""
    upload_ids: list[str] = []
    try:
        yield upload_ids
    finally:
        client = s3._get_s3_client()
        for up_id in upload_ids:
            listed = client.list_objects_v2(Bucket=_S3_BUCKET, Prefix=f"uploads/{up_id}/")
            for obj in listed.get("Contents", []):
                key = obj.get("Key")
                if key:
                    client.delete_object(Bucket=_S3_BUCKET, Key=key)


def _stage_upload(upload_ids: list[str], body: bytes) -> str:
    """Write *body* to ``uploads/{upload_id}/file`` and track it for cleanup."""
    up_id = f"it-{uuid.uuid4().hex}"
    s3.put_bytes(keys.upload_file(up_id), body, content_type="text/csv")
    upload_ids.append(up_id)
    return up_id


@pytest.fixture()
def track_created() -> Iterator[list[str]]:
    """Collect dataset ids created by create_from_upload and clean them up."""
    ids: list[str] = []
    try:
        yield ids
    finally:
        if ids:
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
                listed = client.list_objects_v2(
                    Bucket=_S3_BUCKET, Prefix=keys.dataset_prefix(ds_id)
                )
                for obj in listed.get("Contents", []):
                    key = obj.get("Key")
                    if key:
                        client.delete_object(Bucket=_S3_BUCKET, Key=key)


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


def _dataset_row(ds_id: str) -> tuple[str, str, int, bool]:
    """Return ``(source_type, status, data_version, has_url)`` from PostgreSQL."""
    with core_db.session_scope() as session_db:
        row = session_db.execute(
            text(
                "SELECT source_type, status, data_version, "
                "(original_url IS NOT NULL OR normalized_url IS NOT NULL) "
                "FROM datasets WHERE id = CAST(:id AS uuid)"
            ),
            {"id": ds_id},
        ).one()
    return str(row[0]), str(row[1]), int(row[2]), bool(row[3])


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


def _dataset_name(ds_id: str) -> str:
    with core_db.session_scope() as session_db:
        return str(
            session_db.execute(
                text("SELECT name FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# valid upload → dataset v1 (Requirement 7.4)
# ---------------------------------------------------------------------------


def test_valid_upload_creates_dataset_v1(
    upload_factory: list[str],
    track_created: list[str],
    _clean_queue: None,
) -> None:
    """A valid upload → upload dataset v1, file + mapping copied, enqueued."""
    csv_body = b"review,stars,date\nGreat product,5,2024-01-02\nWould buy again,4,2024-02-10\n"
    up_id = _stage_upload(upload_factory, csv_body)

    dataset_id = service.create_from_upload(
        up_id,
        "Acme reviews export",
        {"text": "review", "rating": "stars", "date": "date"},
        "Exported from Acme admin",
    )
    track_created.append(dataset_id)

    # datasets row: source_type=upload, status requested, no URL, v1.
    source_type, status, version, has_url = _dataset_row(dataset_id)
    assert source_type == SourceType.UPLOAD.value
    assert status == DatasetStatus.REQUESTED.value
    assert version == 1
    assert has_url is False
    assert _dataset_name(dataset_id) == "Acme reviews export"

    # v1 dataset_versions row with trigger `initial`.
    assert _version_rows(dataset_id) == [(1, "initial")]

    # File copied to raw/v1/upload.csv; mapping.json written with keep rule.
    assert s3.object_exists(keys.dataset_raw_upload(dataset_id, 1))
    assert s3.object_exists(keys.dataset_raw_mapping(dataset_id, 1))
    assert s3.get_bytes(keys.dataset_raw_upload(dataset_id, 1)) == csv_body
    mapping_doc = json.loads(s3.get_bytes(keys.dataset_raw_mapping(dataset_id, 1)))
    assert mapping_doc["mapping"] == {"text": "review", "rating": "stars", "date": "date"}
    # A date column is mapped → most_recent_by_date keep rule (Requirement 7.6).
    assert mapping_doc["keep_rule"] == "most_recent_by_date"
    assert mapping_doc["usable_rows"] == 2
    assert mapping_doc["will_keep"] == 2

    # Exactly one processing message carrying IDs only was enqueued.
    assert _drain_processing_queue() == [{"dataset_id": dataset_id, "data_version": 1}]


# ---------------------------------------------------------------------------
# over-limit upload → preview keep rule in mapping.json (Requirement 7.6)
# ---------------------------------------------------------------------------


def test_over_limit_upload_records_keep_rule(
    upload_factory: list[str],
    track_created: list[str],
    _clean_queue: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file with more usable rows than MAX_REVIEWS records will_keep=MAX_REVIEWS."""
    # Shrink MAX_REVIEWS so a tiny fixture is "over the limit".
    monkeypatch.setenv("MAX_REVIEWS", "2")
    get_settings.cache_clear()

    csv_body = (
        b"review,when\nfirst,2024-01-01\nsecond,2024-01-02\nthird,2024-01-03\nfourth,2024-01-04\n"
    )
    up_id = _stage_upload(upload_factory, csv_body)

    dataset_id = service.create_from_upload(up_id, "Over limit", {"text": "review", "date": "when"})
    track_created.append(dataset_id)

    mapping_doc = json.loads(s3.get_bytes(keys.dataset_raw_mapping(dataset_id, 1)))
    assert mapping_doc["usable_rows"] == 4
    assert mapping_doc["will_keep"] == 2  # capped at MAX_REVIEWS
    assert mapping_doc["keep_rule"] == "most_recent_by_date"

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# invalid upload → deleted, nothing created (Requirement 7.3)
# ---------------------------------------------------------------------------


def test_invalid_upload_is_deleted_and_creates_nothing(
    upload_factory: list[str],
    _clean_queue: None,
) -> None:
    """A file with no usable text column → UploadInvalidError, object deleted."""
    csv_body = b"name,stars\nAlice,5\nBob,4\n"  # no review-text column
    up_id = _stage_upload(upload_factory, csv_body)

    before = _count_datasets()
    with pytest.raises(UploadInvalidError):
        service.create_from_upload(up_id, "No text column", {})

    # The staged object was deleted by the parser (Requirement 7.3).
    assert s3.object_exists(keys.upload_file(up_id)) is False
    # No dataset was created and nothing was enqueued.
    assert _count_datasets() == before
    assert _drain_processing_queue() == []


def _count_datasets() -> int:
    with core_db.session_scope() as session_db:
        return int(session_db.execute(text("SELECT count(*) FROM datasets")).scalar_one())
