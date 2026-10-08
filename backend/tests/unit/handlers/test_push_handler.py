"""Unit tests for app.handlers.push.PushHandler (dataset-library task 4.2).

These prove the fan-out and stale-connection cleanup logic without a live
WebSocket API. The ``ws-connections`` table is backed by moto (a real in-memory
DynamoDB), and the API Gateway Management API ``post_to_connection`` call is
stubbed with a fake client that records targets and can be told to return
``GoneException`` for specific connections.

Covered:
* both event types broadcast to every connection (Requirements 6.2, 6.5);
* a ``GoneException`` (410) deletes that connection's row (Requirement 6.5);
* a non-410 send error does not block delivery to the other connections and
  does not delete the row (design "Error Handling");
* an unexpected detail-type is dropped without a send;
* an empty connection set is a no-op;
* the client frame carries ``type`` plus the event detail.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from app.consumer import MessageMeta
from app.core.config import get_settings
from app.handlers import push as push_mod
from app.handlers.push import PushHandler
from app.realtime import connections
from botocore.exceptions import ClientError
from moto import mock_aws

from tests.support.dynamodb import ensure_ws_connections_table

_TABLE = "ws-connections"
_REGION = "us-east-1"


def _meta() -> MessageMeta:
    return MessageMeta(message_id="push-msg", receive_count=1)


class _FakeMgmtClient:
    """Records post_to_connection targets; raises GoneException on demand."""

    def __init__(self, gone: set[str] | None = None, boom: set[str] | None = None) -> None:
        self.gone = gone or set()
        self.boom = boom or set()
        self.posted: list[tuple[str, bytes]] = []

    def post_to_connection(self, *, ConnectionId: str, Data: bytes) -> None:  # noqa: N803
        if ConnectionId in self.gone:
            raise ClientError(
                {"Error": {"Code": "GoneException", "Message": "gone"}},
                "PostToConnection",
            )
        if ConnectionId in self.boom:
            raise ClientError(
                {"Error": {"Code": "ForbiddenException", "Message": "nope"}},
                "PostToConnection",
            )
        self.posted.append((ConnectionId, Data))


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run against moto DynamoDB and reset memoised handles between tests."""
    monkeypatch.setenv("WS_CONNECTIONS_TABLE", _TABLE)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    get_settings.cache_clear()
    connections.reset_client()
    push_mod.reset_client()
    with mock_aws():
        ensure_ws_connections_table(_TABLE, region=_REGION)
        yield
    get_settings.cache_clear()
    connections.reset_client()
    push_mod.reset_client()


def _seed(*connection_ids: str) -> None:
    for cid in connection_ids:
        connections.add_connection(cid)


def _use_fake(monkeypatch: pytest.MonkeyPatch, fake: _FakeMgmtClient) -> None:
    monkeypatch.setattr(push_mod, "_get_mgmt_client", lambda: fake)


@pytest.mark.parametrize("detail_type", ["dataset.status.changed", "check.updated"])
def test_broadcasts_both_event_types_to_every_connection(
    monkeypatch: pytest.MonkeyPatch, detail_type: str
) -> None:
    _seed("c1", "c2", "c3")
    fake = _FakeMgmtClient()
    _use_fake(monkeypatch, fake)

    body: dict[str, Any] = {"detail-type": detail_type, "detail": {"dataset_id": "d1"}}
    PushHandler().handle(body, _meta())

    assert {cid for cid, _ in fake.posted} == {"c1", "c2", "c3"}
    # Every frame carries the event type plus the detail payload.
    for _, data in fake.posted:
        frame = json.loads(data)
        assert frame["type"] == detail_type
        assert frame["dataset_id"] == "d1"


def test_gone_connection_is_cleaned_up(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed("live", "stale")
    fake = _FakeMgmtClient(gone={"stale"})
    _use_fake(monkeypatch, fake)

    PushHandler().handle(
        {"detail-type": "dataset.status.changed", "detail": {"dataset_id": "d1"}},
        _meta(),
    )

    # The live connection still received the frame.
    assert [cid for cid, _ in fake.posted] == ["live"]
    # The stale (410) connection's row was deleted; the live one remains.
    remaining = set(connections.list_connection_ids())
    assert remaining == {"live"}


def test_non_gone_error_does_not_block_others_or_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed("ok1", "bad", "ok2")
    fake = _FakeMgmtClient(boom={"bad"})
    _use_fake(monkeypatch, fake)

    PushHandler().handle(
        {"detail-type": "check.updated", "detail": {"check_id": "c1"}},
        _meta(),
    )

    # The two healthy connections still got the frame.
    assert {cid for cid, _ in fake.posted} == {"ok1", "ok2"}
    # A non-410 error leaves the row in place (only 410 means "gone").
    assert set(connections.list_connection_ids()) == {"ok1", "bad", "ok2"}


def test_unexpected_detail_type_is_dropped_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed("c1")
    fake = _FakeMgmtClient()
    _use_fake(monkeypatch, fake)

    PushHandler().handle(
        {"detail-type": "chat.exchange.saved", "detail": {"dataset_id": "d1"}},
        _meta(),
    )

    assert fake.posted == []


def test_no_connections_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeMgmtClient()
    _use_fake(monkeypatch, fake)

    # No seeded connections: nothing to post, and no crash.
    PushHandler().handle(
        {"detail-type": "dataset.status.changed", "detail": {}},
        _meta(),
    )
    assert fake.posted == []
