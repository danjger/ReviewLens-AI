"""Unit tests for the shared schema-repair helper (``app.worker.ai._repair``).

Task 4.4 factors the "call → parse → on invalid, repair-call → parse" skeleton
out of the three AI tasks into :func:`app.worker.ai._repair.call_parse_repair`.
These tests exercise the helper directly with simple stand-in ``create`` and
``parse`` callables, so its two guarantees are verified once, independent of any
one task's fallback:

- A first parse that raises the task's invalid error triggers exactly one repair
  call; the repaired result is returned when it parses.
- An ``AIUnavailable`` raised by ``create`` is never caught — it propagates from
  either the first or the repair call, and no repair retry follows a first-call
  ``AIUnavailable`` (Requirement 7.4).

The repair prompt is a versioned file (``prompts/repair_v1.md``); its assembly
is checked via :func:`build_repair_user`.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.extraction.errors import AIUnavailable
from app.worker.ai import _repair


class _InvalidError(Exception):
    """Stand-in for a task's XInvalidError (e.g. ProfileInvalidError)."""


def _parse_ok(response: Any) -> str:  # noqa: ANN401
    """Parse that returns the response unchanged (a valid parse)."""
    return str(response)


def _parse_always_invalid(response: Any) -> str:  # noqa: ANN401, ARG001
    raise _InvalidError("always invalid")


class _Create:
    """Records (system, user) calls and returns/raises queued items in order."""

    def __init__(self, items: list[Any]) -> None:  # noqa: ANN401
        self._items = list(items)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str) -> Any:  # noqa: ANN401
        self.calls.append((system, user))
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestBuildRepairUser:
    def test_appends_reason_and_schema_instruction(self) -> None:
        built = _repair.build_repair_user("ORIGINAL BODY", "field 'name' is required")
        assert built.startswith("ORIGINAL BODY")
        assert "field 'name' is required" in built
        assert "schema" in built.lower()

    def test_repair_prompt_version_is_v1(self) -> None:
        assert _repair.REPAIR_PROMPT_VERSION == "repair_v1"


class TestCallParseRepair:
    def test_first_parse_valid_makes_one_call(self) -> None:
        create = _Create(["resp1"])

        result = _repair.call_parse_repair(
            create=create,
            parse=_parse_ok,
            invalid_error=_InvalidError,
            system="SYS",
            user="USER",
        )

        assert result == "resp1"
        assert len(create.calls) == 1

    def test_first_invalid_then_repair_valid_returns_repaired(self) -> None:
        create = _Create(["bad", "good"])
        parses: list[str] = []

        def parse(response: Any) -> str:  # noqa: ANN401
            parses.append(str(response))
            if response == "bad":
                raise _InvalidError("bad output")
            return str(response)

        result = _repair.call_parse_repair(
            create=create,
            parse=parse,
            invalid_error=_InvalidError,
            system="SYS",
            user="USER",
        )

        assert result == "good"
        assert len(create.calls) == 2
        # The repair call appended the repair instruction to the original user.
        first_user = create.calls[0][1]
        repair_user = create.calls[1][1]
        assert first_user == "USER"
        assert repair_user.startswith("USER")
        assert "bad output" in repair_user  # reason threaded through

    def test_both_invalid_reraises_invalid_error(self) -> None:
        create = _Create(["bad1", "bad2"])

        with pytest.raises(_InvalidError):
            _repair.call_parse_repair(
                create=create,
                parse=_parse_always_invalid,
                invalid_error=_InvalidError,
                system="SYS",
                user="USER",
            )

        assert len(create.calls) == 2  # exactly one repair retry

    def test_ai_unavailable_on_first_call_propagates_without_repair(self) -> None:
        create = _Create([AIUnavailable("down"), "would-be-good"])

        with pytest.raises(AIUnavailable):
            _repair.call_parse_repair(
                create=create,
                parse=_parse_ok,
                invalid_error=_InvalidError,
                system="SYS",
                user="USER",
            )

        assert len(create.calls) == 1  # no repair retry after AIUnavailable

    def test_ai_unavailable_on_repair_call_propagates(self) -> None:
        create = _Create(["bad", AIUnavailable("down on repair")])

        with pytest.raises(AIUnavailable):
            _repair.call_parse_repair(
                create=create,
                parse=_parse_always_invalid,
                invalid_error=_InvalidError,
                system="SYS",
                user="USER",
            )

        assert len(create.calls) == 2
