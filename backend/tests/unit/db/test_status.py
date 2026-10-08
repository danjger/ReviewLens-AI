"""Unit tests for app.db.status (transition / log_event).

Covers platform-foundation task 5.3 and Requirement 4.4:

- ``transition`` updates the ``status`` column AND appends a matching event to
  ``status_detail.events`` in one transaction.
- ``transition`` publishes a ``dataset.status.changed`` event to EventBridge
  with the right source, detail-type, and payload (asserted with moto).
- ``log_event`` appends a progress event WITHOUT changing the status column and
  WITHOUT publishing an event.
- The event entry shape is ``{status, at, message, data}`` with an ISO-8601
  UTC timestamp.

No real database is used here: the row's ``status_detail`` / ``status`` live in
a tiny in-memory fake that mimics the one statement :func:`app.db.status.
_append_event` runs (lock + append) and the status column write. The ordered,
append-only behaviour against *real* PostgreSQL is covered by the integration
test (``tests/integration/db/test_status.py``) and the property test.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from unittest.mock import patch

import boto3
import pytest
from app.db import status as status_mod
from app.db.models import DatasetStatus
from moto import mock_aws

_REGION = "us-east-1"
_BUS = "default"
_DATASET_ID = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# In-memory fake for the dataset row + session
# ---------------------------------------------------------------------------


class FakeDataset:
    """Just the fields app.db.status reads/writes on a Dataset row."""

    def __init__(self) -> None:
        self.id = _DATASET_ID
        self.status = DatasetStatus.REQUESTED
        self.status_detail: dict[str, Any] = {"events": []}
        self.updated_at = datetime(2020, 1, 1)
        self.data_version = 2
        self.active_version = 1
        self.metrics: dict[str, Any] | None = {"review_count": 7}


class FakeSession:
    """A minimal Session that services the exact calls app.db.status makes.

    ``_append_event`` runs a ``SELECT ... FOR UPDATE`` (returns a row when the
    dataset exists) followed by an ``UPDATE ... jsonb_set(... || :event)``. This
    fake interprets those two statements against ``dataset.status_detail`` so
    the append and ordering can be asserted without PostgreSQL.
    """

    def __init__(self, dataset: FakeDataset | None) -> None:
        self._dataset = dataset

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        if "FOR UPDATE" in sql:
            return _FakeResult(None if self._dataset is None else (1,))
        if "jsonb_set" in sql:
            assert self._dataset is not None
            events = self._dataset.status_detail.setdefault("events", [])
            # params["event"] is a one-element list, matching the real `|| :event`.
            events.extend((params or {})["event"])
            return _FakeResult(None)
        raise AssertionError(f"unexpected statement: {sql}")

    def get(self, _model: Any, _pk: str) -> FakeDataset | None:
        return self._dataset


class _FakeResult:
    def __init__(self, first: Any) -> None:
        self._first = first

    def first(self) -> Any:
        return self._first


@contextmanager
def _fake_scope(dataset: FakeDataset | None) -> Any:
    yield FakeSession(dataset)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point config at the local/default bus and reset cached singletons."""
    from app.core.config import get_settings
    from app.events import publisher

    monkeypatch.setenv("EVENTBRIDGE_BUS_NAME", _BUS)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("DB_RESOURCE_ARN", "")
    monkeypatch.setenv("DB_SECRET_ARN", "")
    get_settings.cache_clear()
    publisher.reset_client()
    yield
    get_settings.cache_clear()
    publisher.reset_client()


# ---------------------------------------------------------------------------
# Pure event-shape helper
# ---------------------------------------------------------------------------


def test_build_event_shape_for_status() -> None:
    event = status_mod._build_event(
        status="processing", message="hi", at="2024-01-01T00:00:00+00:00", data={"k": 1}
    )
    assert event == {
        "status": "processing",
        "at": "2024-01-01T00:00:00+00:00",
        "message": "hi",
        "data": {"k": 1},
    }


def test_build_event_defaults_data_to_empty_dict() -> None:
    event = status_mod._build_event(status=None, message="progress", at="x", data=None)
    assert event["data"] == {}
    assert event["status"] is None


# ---------------------------------------------------------------------------
# log_event: appends, no status change, no publish
# ---------------------------------------------------------------------------


def test_log_event_appends_without_changing_status() -> None:
    ds = FakeDataset()
    with (
        patch.object(status_mod, "session_scope", lambda: _fake_scope(ds)),
        patch.object(status_mod, "publish_event") as pub,
    ):
        status_mod.log_event(_DATASET_ID, "Fetching page 3 of 10", extra={"page": 3})

    assert ds.status is DatasetStatus.REQUESTED  # unchanged
    assert len(ds.status_detail["events"]) == 1
    ev = ds.status_detail["events"][0]
    assert ev["status"] is None
    assert ev["message"] == "Fetching page 3 of 10"
    assert ev["data"] == {"page": 3}
    # A progress event is not a status change: nothing published.
    pub.assert_not_called()


def test_log_event_timestamp_is_iso8601_utc() -> None:
    ds = FakeDataset()
    with (
        patch.object(status_mod, "session_scope", lambda: _fake_scope(ds)),
        patch.object(status_mod, "publish_event"),
    ):
        status_mod.log_event(_DATASET_ID, "progress")
    at = ds.status_detail["events"][0]["at"]
    parsed = datetime.fromisoformat(at)
    assert parsed.tzinfo is not None  # timezone-aware, UTC


def test_log_event_missing_dataset_raises() -> None:
    with (
        patch.object(status_mod, "session_scope", lambda: _fake_scope(None)),
        patch.object(status_mod, "publish_event"),
    ):
        with pytest.raises(ValueError, match="does not exist"):
            status_mod.log_event(_DATASET_ID, "progress")


# ---------------------------------------------------------------------------
# transition: status change + append + publish (moto EventBridge)
# ---------------------------------------------------------------------------


def test_transition_updates_status_and_appends_event() -> None:
    ds = FakeDataset()
    with mock_aws():
        # The "default" bus always exists in moto/EventBridge; no creation needed.
        with patch.object(status_mod, "session_scope", lambda: _fake_scope(ds)):
            status_mod.transition(
                _DATASET_ID, DatasetStatus.PROCESSING, "Started", extra={"version": 2}
            )

    # Status column updated.
    assert ds.status is DatasetStatus.PROCESSING
    assert ds.updated_at.year >= 2024  # refreshed
    # Event appended, matching the new status (Property 5: last status event
    # equals the status column).
    assert len(ds.status_detail["events"]) == 1
    ev = ds.status_detail["events"][0]
    assert ev["status"] == "processing"
    assert ev["message"] == "Started"
    assert ev["data"] == {"version": 2}


def test_transition_publishes_dataset_status_changed_with_payload() -> None:
    """The published event has the right source/detail-type and payload."""
    ds = FakeDataset()
    captured: dict[str, Any] = {}

    real_publish = status_mod.publish_event

    def _spy(detail_type: str, detail: dict[str, Any]) -> None:
        captured["detail_type"] = detail_type
        captured["detail"] = detail
        real_publish(detail_type, detail)

    with mock_aws():
        # Capture via EventBridge rule → SQS so we can read the delivered event.
        with (
            patch.object(status_mod, "session_scope", lambda: _fake_scope(ds)),
            patch.object(status_mod, "publish_event", _spy),
        ):
            status_mod.transition(_DATASET_ID, DatasetStatus.UPDATED, "Done", extra={"note": "ok"})

    assert captured["detail_type"] == "dataset.status.changed"
    detail = captured["detail"]
    assert detail == {
        "dataset_id": _DATASET_ID,
        "status": "updated",
        "at": detail["at"],  # exact timestamp is dynamic
        "data_version": 2,
        "active_version": 1,
        "message": "Done",
        "metrics": {"review_count": 7},
    }
    # Timestamp is ISO-8601 and timezone-aware.
    assert datetime.fromisoformat(detail["at"]).tzinfo is not None


def test_transition_event_reaches_eventbridge_bus() -> None:
    """End-to-end through moto: the event lands on the bus with Source=reviewlens.

    A rule on the bus forwards matching events to an SQS queue; we then read the
    queue and assert the delivered envelope's source, detail-type, and detail.
    """
    ds = FakeDataset()
    with mock_aws():
        events = boto3.client("events", region_name=_REGION)
        sqs = boto3.client("sqs", region_name=_REGION)
        # The default bus already exists in moto; attach a rule → SQS target.
        queue_url = sqs.create_queue(QueueName="status-events")["QueueUrl"]
        queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        events.put_rule(
            Name="capture-status",
            EventPattern=json.dumps({"source": ["reviewlens"]}),
        )
        events.put_targets(
            Rule="capture-status",
            Targets=[{"Id": "sqs", "Arn": queue_arn}],
        )

        with patch.object(status_mod, "session_scope", lambda: _fake_scope(ds)):
            status_mod.transition(_DATASET_ID, DatasetStatus.FAILED, "Boom")

        received = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
        messages = received.get("Messages", [])
        assert len(messages) == 1
        envelope = json.loads(messages[0]["Body"])
        assert envelope["source"] == "reviewlens"
        assert envelope["detail-type"] == "dataset.status.changed"
        assert envelope["detail"]["status"] == "failed"
        assert envelope["detail"]["dataset_id"] == _DATASET_ID
