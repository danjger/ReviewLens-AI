"""Integration tests for Add routing of HTML-upload items (dataset-ingestion task 20.1).

Task 20 adds :func:`app.ingestion.service.create_from_html_upload` and extends
:func:`app.ingestion.service.add_items` so an **HTML-upload** Check item (one
carrying ``upload_id`` in place of a URL) is created or refreshed correctly.
This suite drives ``add_items`` against the **real** backing services (the same
ones ``test_add_service_int.py`` uses):

* the LocalStack ``check-sessions`` DynamoDB table (the Check Session whose
  single item carries ``upload_id``, the stored verdict, and the optional
  ``existing_dataset`` match the Check handler wrote);
* the LocalStack S3 bucket (the uploaded HTML + plan the Check handler copied to
  ``checks/{check_id}/{item_id}/page.html`` / ``plan.json`` — the *same* keys a
  URL capture uses — copied into ``datasets/{id}/raw/v1`` by
  ``create_from_html_upload`` and into ``raw/v{n}`` by the shared Refresh
  Service with trigger ``upload_replace``);
* the Compose PostgreSQL database (the ``datasets`` / ``dataset_versions``
  tables and the native ``source_type`` enum's ``html_upload`` value);
* the LocalStack SQS FIFO ``processing-queue.fifo``.

What it proves (Requirements 8.8, 8.9, 8.11, 8.12, 3.8, 3.9):

1. **no source URL -> one ``html_upload`` dataset v1 + enqueue** — an upload with
   no ``source_url`` ALWAYS creates (Requirement 8.11): a ``source_type =
   html_upload`` row with ``data_version = 1``, a v1 ``dataset_versions`` row
   (trigger ``initial``), null URL columns, no snapshot, the viability verdict
   in ``status_detail``, and one processing message enqueued (Requirement 8.9).
2. **tracked source URL -> no new row, v2 via ``upload_replace``** — an upload
   whose ``source_url`` matched an existing dataset at Check time refreshes that
   dataset through the shared Refresh Service: no new ``datasets`` row, the
   original gains a v2 with trigger ``upload_replace`` and a ``refresh_requested``
   event (Requirement 8.12).
3. **two uploads of the same page, no source URL -> two datasets** — because an
   upload with no source URL never participates in URL dedupe, two such uploads
   of the same page create two independent ``html_upload`` datasets
   (Requirement 8.11).
4. **``wont_work`` refused** — a ``wont_work`` upload item creates nothing
   (Requirement 3.8).
5. **``limited`` needs confirmation** — a ``limited`` upload item without
   ``confirm_limited`` is ``needs_confirmation`` and creates nothing
   (Requirement 3.9); with confirmation it creates.

Add itself never calls the AI (the verdict and capture already exist), so the
process-wide offline stub from ``conftest.py`` guarantees no network call.

Running
-------
Run under ``make up`` (needs LocalStack **and** the Compose PostgreSQL) with the
worker consumers STOPPED and a fresh DB. The module and each test skip cleanly
when either backing service is unavailable. Each test uses its own random check
id / dataset ids and cleans up its DynamoDB rows, S3 prefixes, and database
rows, and drains the processing queue.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import httpx
import pytest
from app.core import db as core_db
from app.core.config import get_settings
from app.db.models import Base, Dataset, DatasetStatus, SourceType
from app.ingestion import check_session, service
from app.ingestion.check_session import CheckItem
from app.ingestion.service import AddRequestItem
from app.ingestion.url_normalizer import normalize
from app.storage import keys, s3
from sqlalchemy import text

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"
_CHECK_SESSIONS_TABLE = "check-sessions"
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
    # Clear the origin secret so OriginGuardMiddleware runs in local-dev bypass,
    # matching the add/check integration suites (task 12.3 / Known Issues D).
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
        pytest.skip("LocalStack not reachable; run under the Compose stack")


@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    """Ensure the DB schema exists; skip when no PostgreSQL is reachable."""
    get_settings.cache_clear()
    core_db.reset_engine()
    if get_settings().is_aws or not _database_reachable():
        pytest.skip("PostgreSQL not reachable; run under the Compose stack")
    engine = core_db.get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(engine)
    yield
    core_db.reset_engine()


# ---------------------------------------------------------------------------
# Capture staging (real S3): an HTML-upload capture is page.html + plan.json
# under the SAME check keys a URL capture uses, with NO snapshot.
# ---------------------------------------------------------------------------


def _stage_upload_capture(check_id: str, item_id: str) -> None:
    """Stage the uploaded HTML + plan the Check handler would have written.

    Mirrors the Check handler's upload branch: it copies the uploaded markup to
    ``checks/{check_id}/{item_id}/page.html`` and writes the Extraction Plan to
    ``plan.json`` under the same prefix a URL capture uses. **No** snapshot is
    produced for an upload (Requirement 8.10), so none is staged — this proves
    the create/refresh paths tolerate a missing snapshot.
    """
    s3.put_bytes(
        keys.check_page(check_id, item_id),
        b"<html><head><title>Saved Reviews</title></head>"
        b"<body><main>uploaded reviews</main></body></html>",
        content_type="text/html; charset=utf-8",
    )
    s3.put_bytes(
        keys.check_plan(check_id, item_id),
        json.dumps({"method": "selectors", "degraded": False}).encode("utf-8"),
        content_type="application/json",
    )


def _verdict(label: str, *, page_title: str | None = None) -> dict[str, object]:
    """A minimal verdict object of the shape viability.assess writes."""
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
def html_check_factory() -> Iterator[Callable[..., tuple[str, str]]]:
    """Create a one-item ``origin="new"`` Check Session for an HTML upload.

    The single item carries ``upload_id`` (so Add takes the HTML-upload path),
    the given verdict, the optional ``source_url``/``normalized``, and an
    optional ``existing_dataset`` match (what the Check handler's duplicate
    lookup would have written when a tracked ``source_url`` was supplied). The
    uploaded HTML + plan are staged under the check prefix. Returns
    ``(check_id, item_id)`` and cleans up DynamoDB rows + the S3 check prefix.
    """
    check_ids: list[str] = []

    def _make(
        *,
        verdict_label: str = "will_work",
        existing_dataset: dict[str, object] | None = None,
        page_title: str | None = "Saved Reviews",
        source_url: str | None = None,
        with_capture: bool = True,
    ) -> tuple[str, str]:
        check_id = f"ith-{uuid.uuid4().hex}"
        item_id = "h1"
        normalized = normalize(source_url) if source_url else None
        item = CheckItem(
            item_id=item_id,
            input="saved-page.html",
            state="done",
            normalized=normalized,
            final_url=None,
            verdict=_verdict(verdict_label, page_title=page_title),
            existing_dataset=existing_dataset,
            upload_id=f"up-{uuid.uuid4().hex}",
            source_url=source_url,
        )
        check_session.put_session(check_id, "new", [item])
        if with_capture:
            _stage_upload_capture(check_id, item_id)
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

    Used by the tracked-source-URL test to seed a dataset whose
    ``normalized_url`` matches the upload's source URL so the Refresh Service
    sees it.
    """
    created: list[str] = []

    def _make(
        *,
        normalized_url: str,
        status: DatasetStatus = DatasetStatus.UPDATED,
        data_version: int = 1,
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
    """Collect dataset ids created by Add during a test and clean them up."""
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


def _dataset_row(ds_id: str) -> tuple[str, int, str, bool]:
    """Return ``(status, data_version, source_type, is_archived)``."""
    with core_db.session_scope() as session_db:
        row = session_db.execute(
            text(
                "SELECT status, data_version, source_type, (archived_at IS NOT NULL) "
                "FROM datasets WHERE id = CAST(:id AS uuid)"
            ),
            {"id": ds_id},
        ).one()
    return str(row[0]), int(row[1]), str(row[2]), bool(row[3])


def _url_columns(ds_id: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Return ``(original_url, final_url, normalized_url, normalized_final_url)``."""
    with core_db.session_scope() as session_db:
        row = session_db.execute(
            text(
                "SELECT original_url, final_url, normalized_url, normalized_final_url "
                "FROM datasets WHERE id = CAST(:id AS uuid)"
            ),
            {"id": ds_id},
        ).one()
    return (
        None if row[0] is None else str(row[0]),
        None if row[1] is None else str(row[1]),
        None if row[2] is None else str(row[2]),
        None if row[3] is None else str(row[3]),
    )


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


def _status_detail(ds_id: str) -> dict[str, Any]:
    with core_db.session_scope() as session_db:
        detail: Any = session_db.execute(
            text("SELECT status_detail FROM datasets WHERE id = CAST(:id AS uuid)"), {"id": ds_id}
        ).scalar_one()
    assert isinstance(detail, dict)
    return detail


def _item_state(check_id: str, item_id: str) -> str:
    session = check_session.get_session(check_id)
    assert session is not None
    return session.items[item_id].state


def _existing_match(ds_id: str, status: str) -> dict[str, object]:
    """The ``existing_dataset`` match shape the check handler stores on an item."""
    return {"id": ds_id, "name": "Existing Acme CRM", "archived": False, "status": status}


# ---------------------------------------------------------------------------
# no source URL -> one html_upload dataset v1 + enqueue (Requirements 8.8, 8.9, 8.11)
# ---------------------------------------------------------------------------


def test_html_upload_no_source_url_creates_one_dataset_v1(
    html_check_factory: Callable[..., tuple[str, str]],
    track_created: list[str],
    _clean_queue: None,
) -> None:
    """An HTML upload with no source URL always creates one html_upload v1.

    _Validates: Requirements 8.8, 8.9, 8.11._
    """
    check_id, item_id = html_check_factory(verdict_label="will_work", source_url=None)

    results = service.add_items(
        check_id,
        [AddRequestItem(item_id, name="Saved G2 Reviews", description="Exported by analyst")],
    )

    assert len(results) == 1
    result = results[0]
    assert result.outcome == "created"
    assert result.dataset_id is not None
    ds_id = result.dataset_id
    track_created.append(ds_id)

    # source_type html_upload, data_version 1, status requested.
    status, version, source_type, is_archived = _dataset_row(ds_id)
    assert source_type == SourceType.HTML_UPLOAD.value
    assert version == 1
    assert status == DatasetStatus.REQUESTED.value
    assert is_archived is False

    # No source URL supplied -> URL columns are all null (out of URL dedupe).
    assert _url_columns(ds_id) == (None, None, None, None)

    # Viability saved in status_detail, requested event present.
    detail = _status_detail(ds_id)
    assert detail["viability"]["verdict"] == "will_work"
    events = detail.get("events", [])
    assert any(e.get("message") == "requested" for e in events)

    # v1 dataset_versions row with trigger `initial`.
    assert _version_rows(ds_id) == [(1, "initial")]

    # Uploaded HTML + plan copied into raw/v1; NO snapshot copied.
    assert s3.object_exists(keys.dataset_raw_page(ds_id, 1, 1))
    assert s3.object_exists(keys.dataset_raw_plan(ds_id, 1))
    assert not s3.object_exists(keys.dataset_snapshot(ds_id, 1))

    # The Check item is now `applied`.
    assert _item_state(check_id, item_id) == "applied"

    # Exactly one processing message carrying IDs only.
    assert _drain_processing_queue() == [{"dataset_id": ds_id, "data_version": 1}]


def test_html_upload_with_source_url_stores_normalized_url(
    html_check_factory: Callable[..., tuple[str, str]],
    track_created: list[str],
    _clean_queue: None,
) -> None:
    """An HTML upload with an UNTRACKED source URL creates and stores it.

    The source URL is stored on ``original_url``/``normalized_url`` for display
    and provenance (``html_upload`` rows are not covered by the URL unique index,
    so storing it is safe). ``final_url``/``normalized_final_url`` stay null.

    _Validates: Requirements 8.8, 8.9._
    """
    source_url = f"https://untracked.example.com/{uuid.uuid4().hex}"
    check_id, item_id = html_check_factory(source_url=source_url)

    results = service.add_items(
        check_id, [AddRequestItem(item_id, name="Saved Reviews", source_url=source_url)]
    )
    assert results[0].outcome == "created"
    ds_id = results[0].dataset_id
    assert ds_id is not None
    track_created.append(ds_id)

    original, final, normalized, normalized_final = _url_columns(ds_id)
    assert original == source_url
    assert normalized == normalize(source_url)
    assert final is None
    assert normalized_final is None


# ---------------------------------------------------------------------------
# tracked source URL -> no new row, v2 via upload_replace (Requirement 8.12)
# ---------------------------------------------------------------------------


def test_html_upload_tracked_source_url_refreshes_without_new_row(
    html_check_factory: Callable[..., tuple[str, str]],
    existing_dataset_factory: Callable[..., str],
    _clean_queue: None,
) -> None:
    """A tracked source URL refreshes the original via upload_replace (no new row).

    _Validates: Requirement 8.12._
    """
    source_url = f"https://tracked.example.com/{uuid.uuid4().hex}"
    normalized = normalize(source_url)
    existing_id = existing_dataset_factory(
        normalized_url=normalized, status=DatasetStatus.UPDATED, data_version=1
    )
    existing = _existing_match(existing_id, "updated")
    check_id, item_id = html_check_factory(source_url=source_url, existing_dataset=existing)

    results = service.add_items(
        check_id, [AddRequestItem(item_id, name="Saved Reviews", source_url=source_url)]
    )

    assert results[0].outcome == "refreshed"
    assert results[0].dataset_id == existing_id

    # No new html_upload row was created for this source URL.
    with core_db.session_scope() as session_db:
        html_rows = int(
            session_db.execute(
                text(
                    "SELECT count(*) FROM datasets "
                    "WHERE source_type = 'html_upload' AND normalized_url = :u"
                ),
                {"u": normalized},
            ).scalar_one()
        )
    assert html_rows == 0

    # The original dataset gained v2 with trigger upload_replace + a
    # refresh_requested event; status back to requested.
    status, version, _, _ = _dataset_row(existing_id)
    assert version == 2
    assert status == DatasetStatus.REQUESTED.value
    assert (2, "upload_replace") in _version_rows(existing_id)
    events = _status_detail(existing_id).get("events", [])
    assert any(e.get("message") == "refresh_requested" for e in events)

    # The uploaded capture was copied into the new version's raw/v2.
    assert s3.object_exists(keys.dataset_raw_page(existing_id, 2, 1))
    assert s3.object_exists(keys.dataset_raw_plan(existing_id, 2))

    assert _drain_processing_queue() == [{"dataset_id": existing_id, "data_version": 2}]


# ---------------------------------------------------------------------------
# two uploads of the same page, no source URL -> two datasets (Requirement 8.11)
# ---------------------------------------------------------------------------


def test_two_html_uploads_no_source_url_create_two_datasets(
    html_check_factory: Callable[..., tuple[str, str]],
    track_created: list[str],
    _clean_queue: None,
) -> None:
    """Two uploads of the same page with no source URL -> two datasets.

    An HTML upload without a source URL has a null ``normalized_url`` and never
    participates in URL dedupe, so two uploads of the same saved page create two
    independent datasets.

    _Validates: Requirement 8.11._
    """
    check_a, item_a = html_check_factory(source_url=None)
    check_b, item_b = html_check_factory(source_url=None)

    first = service.add_items(check_a, [AddRequestItem(item_a, name="Upload A")])
    second = service.add_items(check_b, [AddRequestItem(item_b, name="Upload B")])

    assert first[0].outcome == "created"
    assert second[0].outcome == "created"
    id_a = first[0].dataset_id
    id_b = second[0].dataset_id
    assert id_a is not None and id_b is not None
    track_created.extend([id_a, id_b])

    # Two distinct datasets, both html_upload with null normalized_url.
    assert id_a != id_b
    for ds_id in (id_a, id_b):
        _, version, source_type, _ = _dataset_row(ds_id)
        assert source_type == SourceType.HTML_UPLOAD.value
        assert version == 1
        assert _url_columns(ds_id)[2] is None  # normalized_url null

    bodies = _drain_processing_queue()
    assert sorted(b["dataset_id"] for b in bodies) == sorted([id_a, id_b])  # type: ignore[type-var]


# ---------------------------------------------------------------------------
# wont_work refused (Requirement 3.8)
# ---------------------------------------------------------------------------


def test_html_upload_wont_work_is_refused(
    html_check_factory: Callable[..., tuple[str, str]],
    _clean_queue: None,
) -> None:
    """A `wont_work` HTML upload is refused; nothing is created (3.8)."""
    check_id, item_id = html_check_factory(verdict_label="wont_work", source_url=None)

    results = service.add_items(check_id, [AddRequestItem(item_id, name="Blocked page")])

    assert results[0].outcome == "refused_wont_work"
    assert results[0].dataset_id is None
    assert _drain_processing_queue() == []
    assert _item_state(check_id, item_id) == "done"  # never claimed/applied


# ---------------------------------------------------------------------------
# limited needs confirmation (Requirement 3.9)
# ---------------------------------------------------------------------------


def test_html_upload_limited_needs_confirmation(
    html_check_factory: Callable[..., tuple[str, str]],
    _clean_queue: None,
) -> None:
    """A `limited` HTML upload without confirm_limited -> needs_confirmation (3.9)."""
    check_id, item_id = html_check_factory(verdict_label="limited", source_url=None)

    results = service.add_items(
        check_id, [AddRequestItem(item_id, name="Limited page", confirm_limited=False)]
    )

    assert results[0].outcome == "needs_confirmation"
    assert results[0].dataset_id is None
    assert _drain_processing_queue() == []
    assert _item_state(check_id, item_id) == "done"


def test_html_upload_limited_with_confirmation_creates(
    html_check_factory: Callable[..., tuple[str, str]],
    track_created: list[str],
    _clean_queue: None,
) -> None:
    """A `limited` HTML upload WITH confirm_limited is created (3.9)."""
    check_id, item_id = html_check_factory(verdict_label="limited", source_url=None)

    results = service.add_items(
        check_id, [AddRequestItem(item_id, name="Limited page", confirm_limited=True)]
    )

    assert results[0].outcome == "created"
    ds_id = results[0].dataset_id
    assert ds_id is not None
    track_created.append(ds_id)
    _, _, source_type, _ = _dataset_row(ds_id)
    assert source_type == SourceType.HTML_UPLOAD.value
    assert _drain_processing_queue() == [{"dataset_id": ds_id, "data_version": 1}]
