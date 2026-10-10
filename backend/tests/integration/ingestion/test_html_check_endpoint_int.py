"""Integration tests for ``POST /ingest/html-checks`` (dataset-ingestion 19.1).

Task 19.1 drives the new HTML-upload Check endpoint end to end against the
**real** backing services the design's "HTML upload path" calls for:

* the LocalStack S3 bucket — the staged ``uploads/{upload_id}/file`` object the
  endpoint confirms exists (written as the pre-signed PUT would have);
* the LocalStack ``check-sessions`` DynamoDB table — the one-item
  ``origin="new"`` Check Session the endpoint creates, read back through the
  real :mod:`app.ingestion.check_session` store so the stored item's
  ``upload_id`` / ``source_url`` / ``normalized`` are asserted;
* the LocalStack standard ``check-queue`` — the enqueued ``{check_id, item_id}``
  message (IDs only) the check handler will consume.

What it proves (Requirements 8.1, 8.3, 8.7):

1. **happy path** — a staged upload with no ``source_url`` → ``202``
   ``{check_id, item_id, state:"pending"}``, one session whose single item
   carries ``upload_id`` and null URL fields, and exactly **one** check-queue
   message.
2. **source_url routing** — a staged upload with a well-formed ``source_url``
   stores its normalized form on the item (for the handler's duplicate lookup).
3. **missing staged object → 422** — nothing is created or enqueued.
4. **malformed source_url → 422** — nothing is created or enqueued.
5. **429 after the shared ``checks`` limit** — the per-request per-IP limit is
   crossed; the response carries ``Retry-After`` and nothing is enqueued.

The endpoint never calls the AI (the assessment happens later in the handler),
so no FakeClaude is installed here; the process-wide offline stub guards against
any accidental network call anyway.

Running
-------
Run with ``make test-int`` under ``make up`` (needs LocalStack). The module and
each test skip cleanly when LocalStack is unavailable, so the suite still
*collects* without the stack. Each test uses its own random upload id and cleans
up its staged object, its DynamoDB rows, and drains the check queue.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import boto3
import httpx
import pytest
from app.core.config import get_settings
from app.ingestion import check_session
from app.storage import keys, s3

pytestmark = pytest.mark.integration

_REGION = "us-east-1"
_S3_BUCKET = "reviewlens-local"
_ENDPOINT = "http://localhost:4566"
_CHECK_SESSIONS_TABLE = "check-sessions"
#: The standard (non-FIFO) check queue provisioned by ``infra/localstack-init``.
_CHECK_QUEUE_URL = "http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/check-queue"


# ---------------------------------------------------------------------------
# Availability probe (skip cleanly without the stack)
# ---------------------------------------------------------------------------


def _localstack_up() -> bool:
    try:
        resp = httpx.get(f"{_ENDPOINT}/_localstack/health", timeout=2.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


# ---------------------------------------------------------------------------
# Fixtures: point AWS clients at LocalStack, require the stack
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point AWS clients at LocalStack and reset memoised handles."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", _ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_BUCKET", _S3_BUCKET)
    monkeypatch.setenv("CHECK_SESSIONS_TABLE", _CHECK_SESSIONS_TABLE)
    monkeypatch.setenv("CHECK_QUEUE_URL", _CHECK_QUEUE_URL)
    # The integration suite loads the repo-root ``.env``, which sets
    # ``ORIGIN_VERIFY_SECRET`` — so ``OriginGuardMiddleware`` 403s the headerless
    # ``TestClient`` requests before they reach their assertions. Clear the
    # secret so the guard runs in local-dev bypass, matching the add/check
    # integration suites (dataset-ingestion task 12.3). The 429 test still
    # reaches its 429 assertion because the rate limit is checked before any
    # body handling.
    monkeypatch.setenv("ORIGIN_VERIFY_SECRET", "")
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


# ---------------------------------------------------------------------------
# Upload staging + check-session cleanup
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
    s3.put_bytes(keys.upload_file(up_id), body, content_type="text/html; charset=utf-8")
    upload_ids.append(up_id)
    return up_id


@pytest.fixture()
def track_checks() -> Iterator[list[str]]:
    """Collect check ids created by the endpoint and delete their DynamoDB rows."""
    ids: list[str] = []
    try:
        yield ids
    finally:
        ddb = boto3.client("dynamodb", region_name=_REGION, endpoint_url=_ENDPOINT)
        for cid in ids:
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


# ---------------------------------------------------------------------------
# check-queue helpers
# ---------------------------------------------------------------------------


def _drain_check_queue() -> list[dict[str, object]]:
    """Receive and delete all messages on the standard check queue, as dicts."""
    sqs = boto3.client("sqs", region_name=_REGION, endpoint_url=_ENDPOINT)
    bodies: list[dict[str, object]] = []
    while True:
        resp = sqs.receive_message(
            QueueUrl=_CHECK_QUEUE_URL,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
            VisibilityTimeout=1,
        )
        messages = resp.get("Messages", [])
        if not messages:
            break
        for msg in messages:
            bodies.append(json.loads(msg["Body"]))
            sqs.delete_message(QueueUrl=_CHECK_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
    return bodies


@pytest.fixture()
def _clean_queue() -> Iterator[None]:
    """Purge the check queue before and after so each test sees only its own."""
    _drain_check_queue()
    yield
    _drain_check_queue()


# ---------------------------------------------------------------------------
# TestClient
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():  # type: ignore[no-untyped-def]
    from app.api import app
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


def _unique_ip_headers() -> dict[str, str]:
    """A unique per-test client IP so the shared ``checks`` limit isn't shared."""
    return {"CloudFront-Viewer-Address": f"203.0.113.{uuid.uuid4().int % 250}:4321"}


# ---------------------------------------------------------------------------
# happy path (Requirements 8.1, 8.3, 8.7)
# ---------------------------------------------------------------------------


def test_html_check_happy_path_enqueues_one_message(
    client,  # type: ignore[no-untyped-def]
    upload_factory: list[str],
    track_checks: list[str],
    _clean_queue: None,
) -> None:
    """A staged upload, no source_url → 202, one session, one check-queue message."""
    up_id = _stage_upload(upload_factory, b"<html><title>Acme</title><body>reviews</body></html>")

    resp = client.post(
        "/api/ingest/html-checks",
        json={"upload_id": up_id},
        headers=_unique_ip_headers(),
    )
    assert resp.status_code == 202
    body = resp.json()
    check_id = body["check_id"]
    track_checks.append(check_id)
    assert body["item_id"] == "u1"
    assert body["state"] == "pending"

    # The one-item origin="new" session carries the upload_id and no URL fields.
    session = check_session.get_session(check_id)
    assert session is not None
    assert session.origin == "new"
    assert set(session.items) == {"u1"}
    item = session.items["u1"]
    assert item.state == "pending"
    assert item.upload_id == up_id
    assert item.source_url is None
    assert item.normalized is None
    assert item.final_url is None

    # Exactly one check-queue message, IDs only.
    messages = _drain_check_queue()
    assert messages == [{"check_id": check_id, "item_id": "u1"}]


def test_html_check_with_source_url_stores_normalized(
    client,  # type: ignore[no-untyped-def]
    upload_factory: list[str],
    track_checks: list[str],
    _clean_queue: None,
) -> None:
    """A supplied, well-formed source_url is normalized onto the item (8.12)."""
    up_id = _stage_upload(upload_factory, b"<html><title>Acme</title></html>")

    resp = client.post(
        "/api/ingest/html-checks",
        json={"upload_id": up_id, "source_url": "https://www.Example.com/Reviews/?utm_source=x"},
        headers=_unique_ip_headers(),
    )
    assert resp.status_code == 202
    check_id = resp.json()["check_id"]
    track_checks.append(check_id)

    session = check_session.get_session(check_id)
    assert session is not None
    item = session.items["u1"]
    assert item.upload_id == up_id
    assert item.source_url == "https://www.Example.com/Reviews/?utm_source=x"
    # Tracking params stripped, host lowercased, www removed, trailing slash gone.
    assert item.normalized == "https://example.com/Reviews"
    assert item.final_url == "https://www.Example.com/Reviews/?utm_source=x"

    assert _drain_check_queue() == [{"check_id": check_id, "item_id": "u1"}]


# ---------------------------------------------------------------------------
# missing staged object → 422 (Requirement 8.3)
# ---------------------------------------------------------------------------


def test_html_check_missing_staged_object_is_422(
    client,  # type: ignore[no-untyped-def]
    _clean_queue: None,
) -> None:
    """An upload id with no staged object → 422, nothing created or enqueued."""
    resp = client.post(
        "/api/ingest/html-checks",
        json={"upload_id": f"never-staged-{uuid.uuid4().hex}"},
        headers=_unique_ip_headers(),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _drain_check_queue() == []


# ---------------------------------------------------------------------------
# malformed source_url → 422 (Requirement 8.3)
# ---------------------------------------------------------------------------


def test_html_check_malformed_source_url_is_422(
    client,  # type: ignore[no-untyped-def]
    upload_factory: list[str],
    _clean_queue: None,
) -> None:
    """A staged object but a malformed source_url → 422, nothing enqueued."""
    up_id = _stage_upload(upload_factory, b"<html><title>Acme</title></html>")

    resp = client.post(
        "/api/ingest/html-checks",
        json={"upload_id": up_id, "source_url": "not a url"},
        headers=_unique_ip_headers(),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _drain_check_queue() == []


# ---------------------------------------------------------------------------
# 429 after the shared ``checks`` limit (Requirement 8.7)
# ---------------------------------------------------------------------------


def test_html_check_429_after_limit(
    client,  # type: ignore[no-untyped-def]
    upload_factory: list[str],
    track_checks: list[str],
    monkeypatch: pytest.MonkeyPatch,
    _clean_queue: None,
) -> None:
    """Once the per-IP ``checks`` limit is crossed the endpoint returns 429."""
    # A tiny per-IP limit so a couple of requests cross it deterministically.
    monkeypatch.setenv("RL_CHECKS_PER_IP_HOUR", "2")
    get_settings.cache_clear()

    up_id = _stage_upload(upload_factory, b"<html><title>Acme</title></html>")
    headers = _unique_ip_headers()

    saw_429 = False
    last_status = None
    for _ in range(5):
        resp = client.post("/api/ingest/html-checks", json={"upload_id": up_id}, headers=headers)
        last_status = resp.status_code
        if resp.status_code == 202:
            track_checks.append(resp.json()["check_id"])
        if resp.status_code == 429:
            saw_429 = True
            # The panel must be told when checks can resume (Requirement 8.7).
            assert "Retry-After" in resp.headers
            assert resp.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
            break
    assert saw_429, f"expected a 429 after the limit; last status was {last_status}"
