"""Unit tests for app.handlers.dlq (review-analysis task 1.3).

These cover the DLQ backstop's pure body parsing and its delegation to the
shared :func:`app.jobs.sweep.fail_version`, with that function stubbed so no
database is needed. The real ``failed`` transition and its idempotency against
PostgreSQL are proved by ``tests/integration/jobs/test_sweep_int.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from app.consumer import MAX_RECEIVE_COUNT, MessageMeta
from app.handlers import dlq as dlq_mod
from app.handlers.dlq import DlqHandler, _parse


def _meta() -> MessageMeta:
    return MessageMeta(message_id="dlq-msg", receive_count=MAX_RECEIVE_COUNT)


class TestParse:
    """_parse accepts the processing body shape and rejects malformed ones."""

    def test_data_version_key(self) -> None:
        assert _parse({"dataset_id": "d1", "data_version": 2}) == ("d1", 2)

    def test_version_synonym(self) -> None:
        # `version` is accepted for forward compatibility.
        assert _parse({"dataset_id": "d1", "version": 3}) == ("d1", 3)

    def test_data_version_wins_over_version(self) -> None:
        assert _parse({"dataset_id": "d1", "data_version": 2, "version": 9}) == ("d1", 2)

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"data_version": 1},  # no dataset_id
            {"dataset_id": "", "data_version": 1},  # empty id
            {"dataset_id": "d1"},  # no version
            {"dataset_id": "d1", "data_version": 0},  # non-positive
            {"dataset_id": "d1", "data_version": -1},
            {"dataset_id": "d1", "data_version": "2"},  # non-int
            {"dataset_id": "d1", "data_version": True},  # bool is not a version
            {"check_id": "c1", "item_id": "u1"},  # a check DLQ message
        ],
    )
    def test_rejects_malformed(self, body: dict[str, Any]) -> None:
        assert _parse(body) is None


class TestHandle:
    """handle delegates to fail_version and never crashes on bad input."""

    def test_marks_version_failed(self) -> None:
        with patch.object(dlq_mod, "fail_version", return_value=True) as failer:
            DlqHandler().handle({"dataset_id": "d1", "data_version": 2}, _meta())
        failer.assert_called_once_with("d1", 2, dlq_mod.DLQ_FAILURE_MESSAGE)

    def test_noop_when_already_resolved(self) -> None:
        # fail_version returns False (version already terminal): no crash.
        with patch.object(dlq_mod, "fail_version", return_value=False) as failer:
            DlqHandler().handle({"dataset_id": "d1", "data_version": 2}, _meta())
        failer.assert_called_once()

    def test_malformed_body_is_dropped_without_calling_fail_version(self) -> None:
        with patch.object(dlq_mod, "fail_version") as failer:
            DlqHandler().handle({"check_id": "c1", "item_id": "u1"}, _meta())
        failer.assert_not_called()
