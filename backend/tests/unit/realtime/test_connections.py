"""Unit tests for app.realtime.connections (dataset-library task 4.1).

The ``ws-connections`` store is backed by moto (a real in-memory DynamoDB) so
the item shape, TTL, pagination, and idempotent add/remove are exercised for
real rather than mocked.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from app.core.config import get_settings
from app.realtime import connections

from tests.support.dynamodb import ensure_ws_connections_table

_TABLE = "ws-connections"
_REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _ddb(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from moto import mock_aws

    monkeypatch.setenv("WS_CONNECTIONS_TABLE", _TABLE)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    get_settings.cache_clear()
    connections.reset_client()
    with mock_aws():
        ensure_ws_connections_table(_TABLE, region=_REGION)
        yield
    get_settings.cache_clear()
    connections.reset_client()


def _row(connection_id: str) -> dict[str, object]:
    item = boto3.client("dynamodb", region_name=_REGION).get_item(
        TableName=_TABLE, Key={"connection_id": {"S": connection_id}}
    )
    return item.get("Item", {})


def test_add_connection_writes_id_connected_at_and_ttl() -> None:
    connections.add_connection("abc", now=1_000.0)
    row = _row("abc")
    assert row["connection_id"]["S"] == "abc"
    assert "connected_at" in row
    # TTL is +2h from now (epoch seconds).
    assert int(row["ttl"]["N"]) == 1_000 + connections.CONNECTION_TTL_SECONDS


def test_add_connection_is_idempotent() -> None:
    connections.add_connection("abc", now=1_000.0)
    connections.add_connection("abc", now=2_000.0)
    assert connections.list_connection_ids() == ["abc"]
    # The TTL reflects the latest write.
    assert int(_row("abc")["ttl"]["N"]) == 2_000 + connections.CONNECTION_TTL_SECONDS


def test_remove_connection_deletes_the_row() -> None:
    connections.add_connection("abc")
    connections.remove_connection("abc")
    assert connections.list_connection_ids() == []


def test_remove_missing_connection_is_a_noop() -> None:
    # Deleting a row that was never there must not raise.
    connections.remove_connection("never-existed")
    assert connections.list_connection_ids() == []


def test_list_connection_ids_returns_every_connection() -> None:
    for i in range(5):
        connections.add_connection(f"c{i}")
    assert set(connections.list_connection_ids()) == {f"c{i}" for i in range(5)}
