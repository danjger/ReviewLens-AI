"""Unit tests for the $connect / $disconnect handlers (dataset-library 4.1).

The handlers are thin glue over :mod:`app.realtime.connections`, so these tests
stub the store and assert each handler extracts the connection ID from the
Lambda WebSocket event, calls the right store function, and returns the right
status code — including the malformed-event guards.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from app.realtime import connect as connect_mod
from app.realtime import disconnect as disconnect_mod


def _event(connection_id: str | None) -> dict[str, Any]:
    ctx: dict[str, Any] = {}
    if connection_id is not None:
        ctx["connectionId"] = connection_id
    return {"requestContext": ctx}


class TestConnect:
    def test_stores_connection_and_returns_200(self) -> None:
        with patch.object(connect_mod.connections, "add_connection") as add:
            result = connect_mod.lambda_handler(_event("abc"), None)
        add.assert_called_once_with("abc")
        assert result == {"statusCode": 200}

    def test_missing_connection_id_returns_400_without_storing(self) -> None:
        with patch.object(connect_mod.connections, "add_connection") as add:
            result = connect_mod.lambda_handler(_event(None), None)
        add.assert_not_called()
        assert result == {"statusCode": 400}


class TestDisconnect:
    def test_removes_connection_and_returns_200(self) -> None:
        with patch.object(disconnect_mod.connections, "remove_connection") as rm:
            result = disconnect_mod.lambda_handler(_event("abc"), None)
        rm.assert_called_once_with("abc")
        assert result == {"statusCode": 200}

    def test_missing_connection_id_returns_200_without_removing(self) -> None:
        with patch.object(disconnect_mod.connections, "remove_connection") as rm:
            result = disconnect_mod.lambda_handler(_event(None), None)
        rm.assert_not_called()
        assert result == {"statusCode": 200}
