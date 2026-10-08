"""Unit tests for app.handlers.processing (review-analysis task 1.1).

Task 1.1 is the handler *skeleton* wired to the shared consumer runtime and the
FIFO processing queue: it parses the ``{dataset_id, version}`` body, starts a
15-minute time budget, and hands off to the (not-yet-implemented) pipeline. The
FIFO group/dedup IDs, DLQ, retry count, and visibility window are infrastructure
(CDK) and are asserted by the Workers/Api synth tests, not here.

These tests cover:

- the handler satisfies the ``Handler`` protocol and binds to the ``processing``
  queue, so :mod:`app.consumer` can route to it in both compute modes;
- message-body parsing: a well-formed body yields a ``ProcessingMessage``;
  missing/empty/non-integer/non-positive fields raise ``ValueError`` so the
  runtime DLQs a malformed message rather than dropping work;
- the time budget: a fresh budget has (almost) the full 15 minutes left and is
  not expired; a zero/elapsed budget is expired with no time remaining;
- ``handle`` on a valid body runs without side effects while the pipeline is a
  no-op seam (later subtasks fill it in).

_Validates: Requirements 1.3, 7.1_
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from app.consumer import Handler, MessageMeta
from app.db.models import DatasetStatus
from app.extraction.errors import AIUnavailable
from app.handlers import collection as collection_mod
from app.handlers import completion as completion_mod
from app.handlers import extraction_stage as extraction_stage_mod
from app.handlers import metrics as metrics_mod
from app.handlers import processing as processing_mod
from app.handlers import upload_extraction as upload_extraction_mod
from app.handlers.completion import Outcome
from app.handlers.processing import (
    TIME_BUDGET_SECONDS,
    DatasetState,
    GuardDecision,
    ProcessingHandler,
    ProcessingMessage,
    TimeBudget,
    decide_start,
)
from app.worker.ai import profile as profile_mod


def _meta(receive_count: int = 1) -> MessageMeta:
    return MessageMeta(message_id="m1", receive_count=receive_count)


# A trivial collection result used to stub the collection stage in guard-wiring
# tests (task 2 wired `_collect_pages` into `_run_pipeline`).
_EMPTY_COLLECTION = collection_mod.CollectionResult(
    pages=[], warnings=[], stop_reason=collection_mod.STOP_SKIPPED_UPLOAD
)


# ---------------------------------------------------------------------------
# Protocol / runtime wiring
# ---------------------------------------------------------------------------


class TestHandlerWiring:
    """The module exports a handler bound to the processing queue.

    _Validates: Requirement 1.3_
    """

    def test_module_exports_handler_instance(self) -> None:
        assert isinstance(processing_mod.handler, ProcessingHandler)

    def test_handler_is_bound_to_processing_queue(self) -> None:
        assert processing_mod.handler.queue == "processing"

    def test_handler_satisfies_handler_protocol(self) -> None:
        handler: Handler = ProcessingHandler()
        assert handler.queue == "processing"


# ---------------------------------------------------------------------------
# Message body parsing
# ---------------------------------------------------------------------------


class TestProcessingMessage:
    """``ProcessingMessage.from_body`` parses IDs-only bodies and rejects junk.

    _Validates: Requirement 7.1_
    """

    def test_parses_well_formed_body(self) -> None:
        # Canonical key is ``data_version`` (what producers actually enqueue).
        msg = ProcessingMessage.from_body({"dataset_id": "ds-1", "data_version": 3})
        assert msg == ProcessingMessage(dataset_id="ds-1", version=3)

    def test_parses_legacy_version_synonym(self) -> None:
        # ``version`` is accepted as a backward-compatible synonym.
        msg = ProcessingMessage.from_body({"dataset_id": "ds-1", "version": 3})
        assert msg == ProcessingMessage(dataset_id="ds-1", version=3)

    def test_ignores_extra_keys(self) -> None:
        # Producers may add fields later; only the IDs are required.
        msg = ProcessingMessage.from_body(
            {"dataset_id": "ds-1", "data_version": 1, "trigger": "add"}
        )
        assert msg == ProcessingMessage(dataset_id="ds-1", version=1)

    def test_missing_dataset_id_raises(self) -> None:
        with pytest.raises(ValueError, match="dataset_id"):
            ProcessingMessage.from_body({"data_version": 1})

    def test_missing_version_raises(self) -> None:
        with pytest.raises(ValueError, match="data_version"):
            ProcessingMessage.from_body({"dataset_id": "ds-1"})

    def test_empty_dataset_id_raises(self) -> None:
        with pytest.raises(ValueError, match="dataset_id"):
            ProcessingMessage.from_body({"dataset_id": "", "data_version": 1})

    def test_non_string_dataset_id_raises(self) -> None:
        with pytest.raises(ValueError, match="dataset_id"):
            ProcessingMessage.from_body({"dataset_id": 123, "data_version": 1})

    def test_non_integer_version_raises(self) -> None:
        with pytest.raises(ValueError, match="version"):
            ProcessingMessage.from_body({"dataset_id": "ds-1", "data_version": "2"})

    def test_boolean_version_raises(self) -> None:
        # bool is an int subclass; reject it explicitly so True/False can't pass.
        with pytest.raises(ValueError, match="version"):
            ProcessingMessage.from_body({"dataset_id": "ds-1", "data_version": True})

    def test_non_positive_version_raises(self) -> None:
        with pytest.raises(ValueError, match="version"):
            ProcessingMessage.from_body({"dataset_id": "ds-1", "data_version": 0})


# ---------------------------------------------------------------------------
# Time budget
# ---------------------------------------------------------------------------


class TestTimeBudget:
    """The 15-minute monotonic budget later stages consult.

    _Validates: Requirement 7.1_
    """

    def test_default_budget_is_fifteen_minutes(self) -> None:
        assert TIME_BUDGET_SECONDS == 15 * 60

    def test_fresh_budget_is_not_expired_with_time_left(self) -> None:
        budget = TimeBudget.start()
        assert budget.expired() is False
        # Almost the whole budget remains immediately after starting.
        assert budget.remaining() > TIME_BUDGET_SECONDS - 5
        assert budget.remaining() <= TIME_BUDGET_SECONDS

    def test_elapsed_is_non_negative_and_small_at_start(self) -> None:
        budget = TimeBudget.start()
        assert 0.0 <= budget.elapsed() < 5.0

    def test_zero_budget_is_immediately_expired(self) -> None:
        budget = TimeBudget.start(budget_seconds=0.0)
        assert budget.expired() is True
        assert budget.remaining() == 0.0

    def test_remaining_never_negative_when_overrun(self) -> None:
        # A budget whose start is far in the past is fully consumed.
        import time

        budget = TimeBudget(started_monotonic=time.monotonic() - 10_000, budget_seconds=1.0)
        assert budget.remaining() == 0.0
        assert budget.expired() is True


# ---------------------------------------------------------------------------
# handle() end to end (skeleton)
# ---------------------------------------------------------------------------


class TestHandle:
    """``handle`` parses the body and runs the (no-op) pipeline seam.

    _Validates: Requirements 1.3, 7.1_
    """

    def test_valid_body_runs_without_error(self) -> None:
        # The pipeline now runs the start guard first (task 1.2), so stub the
        # guard out to keep this a pure wiring test of handle().
        handler = ProcessingHandler()
        handler._run_pipeline = lambda *a, **k: None  # type: ignore[method-assign]
        handler.handle({"dataset_id": "ds-1", "data_version": 1}, _meta())

    def test_valid_body_passes_parsed_message_and_budget_to_pipeline(self) -> None:
        handler = ProcessingHandler()
        seen: list[tuple[ProcessingMessage, MessageMeta, TimeBudget]] = []

        def _capture(message: ProcessingMessage, meta: MessageMeta, budget: TimeBudget) -> None:
            seen.append((message, meta, budget))

        handler._run_pipeline = _capture  # type: ignore[method-assign]
        meta = _meta(receive_count=2)
        handler.handle({"dataset_id": "ds-9", "data_version": 4}, meta)

        assert len(seen) == 1
        message, passed_meta, budget = seen[0]
        assert message == ProcessingMessage(dataset_id="ds-9", version=4)
        assert passed_meta is meta
        assert isinstance(budget, TimeBudget)
        assert budget.budget_seconds == TIME_BUDGET_SECONDS

    def test_malformed_body_raises_before_pipeline(self) -> None:
        handler = ProcessingHandler()
        called = False

        def _should_not_run(
            message: ProcessingMessage, meta: MessageMeta, budget: TimeBudget
        ) -> None:
            nonlocal called
            called = True

        handler._run_pipeline = _should_not_run  # type: ignore[method-assign]
        with pytest.raises(ValueError):
            handler.handle({"data_version": 1}, _meta())
        assert called is False


# ---------------------------------------------------------------------------
# Start guard (review-analysis task 1.2)
# ---------------------------------------------------------------------------
#
# These cover the pure decision function `decide_start` (every branch of
# Requirement 1.2) and the `_start_guard` / `_run_pipeline` wiring: a START
# decision transitions requested → processing (through db.status.transition,
# which publishes dataset.status.changed — Requirement 8.1), CONTINUE and SKIP
# make no status change, and SKIP stops the pipeline.
#
# _Validates: Requirements 1.1, 1.2, 8.1


class TestDecideStart:
    """`decide_start` applies Requirement 1.2 to pure inputs.

    _Validates: Requirements 1.1, 1.2_
    """

    def test_requested_current_version_starts(self) -> None:
        state = DatasetState(
            status=DatasetStatus.REQUESTED,
            data_version=1,
            version_completed=False,
            archived=False,
        )
        assert decide_start(1, state) is GuardDecision.START

    def test_processing_same_version_continues(self) -> None:
        # A retry of the version already in flight continues (no-op transition).
        state = DatasetState(
            status=DatasetStatus.PROCESSING,
            data_version=2,
            version_completed=False,
            archived=False,
        )
        assert decide_start(2, state) is GuardDecision.CONTINUE

    def test_older_version_is_skipped(self) -> None:
        # Message version older than data_version → superseded.
        state = DatasetState(
            status=DatasetStatus.PROCESSING,
            data_version=3,
            version_completed=False,
            archived=False,
        )
        assert decide_start(2, state) is GuardDecision.SKIP

    def test_completed_version_is_skipped(self) -> None:
        state = DatasetState(
            status=DatasetStatus.UPDATED,
            data_version=1,
            version_completed=True,
            archived=False,
        )
        assert decide_start(1, state) is GuardDecision.SKIP

    def test_archived_dataset_is_skipped(self) -> None:
        state = DatasetState(
            status=DatasetStatus.REQUESTED,
            data_version=1,
            version_completed=False,
            archived=True,
        )
        assert decide_start(1, state) is GuardDecision.SKIP

    def test_newer_version_than_row_is_skipped(self) -> None:
        # The version owning the increment has not landed yet; nothing to act on.
        state = DatasetState(
            status=DatasetStatus.REQUESTED,
            data_version=1,
            version_completed=False,
            archived=False,
        )
        assert decide_start(2, state) is GuardDecision.SKIP

    def test_updated_current_version_without_completion_row_is_skipped(self) -> None:
        # Defensive: a current-version row that is already terminal but has no
        # recorded outcome is still a no-op.
        state = DatasetState(
            status=DatasetStatus.UPDATED,
            data_version=1,
            version_completed=False,
            archived=False,
        )
        assert decide_start(1, state) is GuardDecision.SKIP

    def test_skip_precedence_over_start(self) -> None:
        # Archived wins even when the status would otherwise START.
        state = DatasetState(
            status=DatasetStatus.REQUESTED,
            data_version=5,
            version_completed=True,
            archived=True,
        )
        assert decide_start(5, state) is GuardDecision.SKIP


# ---------------------------------------------------------------------------
# _start_guard / _run_pipeline wiring (DB + transition patched)
# ---------------------------------------------------------------------------


class _StubResult:
    def __init__(self, first: Any) -> None:
        self._first = first

    def first(self) -> Any:
        return self._first


class _StubSession:
    """Services the two reads `_load_state` runs: the dataset row and the
    dataset_versions outcome. Values are supplied by the test.
    """

    def __init__(
        self,
        *,
        dataset_row: tuple[str, int, bool] | None,
        outcome_row: tuple[Any] | None,
    ) -> None:
        self._dataset_row = dataset_row
        self._outcome_row = outcome_row

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(statement).split())
        if "FROM datasets" in sql:
            return _StubResult(self._dataset_row)
        if "FROM dataset_versions" in sql:
            return _StubResult(self._outcome_row)
        raise AssertionError(f"unexpected statement: {sql}")


@contextmanager
def _stub_scope(session: _StubSession) -> Iterator[_StubSession]:
    yield session


class TestStartGuardWiring:
    """`_start_guard` transitions only on START and `_run_pipeline` stops on SKIP.

    _Validates: Requirements 1.1, 1.2, 8.1_
    """

    def _run(
        self,
        *,
        dataset_row: tuple[str, int, bool] | None,
        outcome_row: tuple[Any] | None,
        version: int = 1,
    ) -> tuple[GuardDecision, list[tuple[Any, ...]]]:
        handler = ProcessingHandler()
        session = _StubSession(dataset_row=dataset_row, outcome_row=outcome_row)
        calls: list[tuple[Any, ...]] = []

        def _transition(dataset_id: str, new_status: Any, message: str, extra: Any = None) -> None:
            calls.append((dataset_id, new_status, message, extra))

        with (
            patch("app.handlers.processing.session_scope", lambda: _stub_scope(session)),
            patch("app.handlers.processing.transition", _transition),
        ):
            decision = handler._start_guard(ProcessingMessage(dataset_id="ds-1", version=version))
        return decision, calls

    def test_start_transitions_to_processing_and_publishes(self) -> None:
        # requested, current version, not completed, not archived → START, and
        # a single transition to processing (which publishes the status event).
        decision, calls = self._run(
            dataset_row=("requested", 1, False),
            outcome_row=None,
        )
        assert decision is GuardDecision.START
        assert len(calls) == 1
        dataset_id, new_status, message, extra = calls[0]
        assert dataset_id == "ds-1"
        assert new_status is DatasetStatus.PROCESSING
        assert message == "processing"
        assert extra == {"version": 1}

    def test_continue_makes_no_transition(self) -> None:
        decision, calls = self._run(
            dataset_row=("processing", 1, False),
            outcome_row=None,
        )
        assert decision is GuardDecision.CONTINUE
        assert calls == []

    def test_skip_completed_version_makes_no_transition(self) -> None:
        decision, calls = self._run(
            dataset_row=("updated", 1, False),
            outcome_row=("updated",),
        )
        assert decision is GuardDecision.SKIP
        assert calls == []

    def test_skip_archived_makes_no_transition(self) -> None:
        decision, calls = self._run(
            dataset_row=("requested", 1, True),
            outcome_row=None,
        )
        assert decision is GuardDecision.SKIP
        assert calls == []

    def test_missing_dataset_raises(self) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            self._run(dataset_row=None, outcome_row=None)

    def test_run_pipeline_stops_on_skip(self) -> None:
        # When the guard skips, _run_pipeline returns without touching the DB
        # beyond the guard read and without any transition.
        handler = ProcessingHandler()
        session = _StubSession(dataset_row=("requested", 2, False), outcome_row=None)
        calls: list[Any] = []
        with (
            patch("app.handlers.processing.session_scope", lambda: _stub_scope(session)),
            patch("app.handlers.processing.transition", lambda *a, **k: calls.append(a)),
        ):
            # version 1 < data_version 2 → SKIP.
            handler._run_pipeline(
                ProcessingMessage(dataset_id="ds-1", version=1),
                _meta(),
                TimeBudget.start(),
            )
        assert calls == []

    def test_run_pipeline_proceeds_on_continue(self) -> None:
        # A retry of the in-flight version continues past the guard (no
        # transition) and runs the remaining stages. The collection stage (task
        # 2) is stubbed so this stays a guard-wiring test: the point is that
        # CONTINUE proceeds past the guard without a status transition.
        handler = ProcessingHandler()
        session = _StubSession(dataset_row=("processing", 1, False), outcome_row=None)
        calls: list[Any] = []
        collected: list[Any] = []
        with (
            patch("app.handlers.processing.session_scope", lambda: _stub_scope(session)),
            patch("app.handlers.processing.transition", lambda *a, **k: calls.append(a)),
            patch.object(
                handler,
                "_collect_pages",
                lambda *a, **k: collected.append(a) or (_EMPTY_COLLECTION, None),
            ),
            # _EMPTY_COLLECTION is a skipped-upload result, so the pipeline runs
            # the upload extraction branch (task 3.2); stub it so this stays a
            # guard-wiring test without reading S3.
            patch.object(
                handler,
                "_extract_upload",
                lambda *a, **k: upload_extraction_mod.UploadExtractionResult(),
            ),
            # Profile (task 4.1) and metrics+output (task 5) run after dedupe;
            # stub them so this guard-wiring test needs no AI client or S3.
            patch.object(
                handler,
                "_build_profile",
                lambda *a, **k: profile_mod.EntityProfile(name="", confidence="low"),
            ),
            patch(
                "app.handlers.processing.metrics.run",
                lambda **k: metrics_mod.MetricsResult(metrics={}, reviews_doc={}, review_count=0),
            ),
            # The completion stage (task 6) ends the version; stub it so this
            # guard-wiring test needs no DB. Its own behaviour is covered by the
            # completion tests.
            patch.object(handler, "_complete", lambda *a, **k: None),
        ):
            handler._run_pipeline(
                ProcessingMessage(dataset_id="ds-1", version=1),
                _meta(),
                TimeBudget.start(),
            )
        # No status transition on CONTINUE, and the pipeline reached collection.
        assert calls == []
        assert len(collected) == 1


# ---------------------------------------------------------------------------
# Completion + final-retry failure wiring (review-analysis task 6)
# ---------------------------------------------------------------------------
#
# These cover `_complete` (building the CompletionDecision + ActualResult and
# delegating to completion.complete) and the `_run_pipeline` final-attempt
# handling: a non-final error propagates (SQS retries); a final-attempt error is
# caught and the version is failed through `_fail_final`; AIUnavailable on the
# final attempt yields the AI-unavailable message. The completion stage's own DB
# behaviour is unit-tested in test_completion.py; here we assert the handler
# wires the right facts into it.
#
# _Validates: Requirements 6.1, 6.2, 6.4, 7.2, 7.4


def _page(method: str = "selectors", *, fallback: bool = False) -> extraction_stage_mod.PageDetail:
    return extraction_stage_mod.PageDetail(
        page=1, url="https://x/1", method=method, found=3, fallback=fallback, discarded=0
    )


class _CompleteSpy:
    """Captures the arguments the handler passes to completion.complete."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return True


class TestCompleteWiring:
    """`_complete` builds the decision/actual from the run's facts.

    _Validates: Requirements 6.1, 6.2, 6.4_
    """

    def test_success_passes_updated_decision_and_actual(self) -> None:
        handler = ProcessingHandler()
        spy = _CompleteSpy()
        metrics_dict = {
            "review_count": 3,
            "pages_captured": 2,
            "extraction": {"method": "selectors"},
        }
        with patch("app.handlers.processing.completion.complete", spy):
            handler._complete(
                ProcessingMessage(dataset_id="ds-1", version=1),
                metrics_dict=metrics_dict,
                review_count=3,
                page_details=[_page(fallback=False), _page("ai_direct", fallback=True)],
                duration_ms=99,
                error=None,
            )
        assert len(spy.calls) == 1
        call = spy.calls[0]
        assert call["decision"].outcome is Outcome.UPDATED
        # Actual result reflects reviews, pages captured, method, and fallbacks.
        actual = call["actual"]
        assert actual.reviews == 3
        assert actual.pages == 2
        assert actual.method == "selectors"
        assert actual.fallbacks == 1
        assert call["duration_ms"] == 99

    def test_zero_reviews_passes_failed_decision(self) -> None:
        handler = ProcessingHandler()
        spy = _CompleteSpy()
        with patch("app.handlers.processing.completion.complete", spy):
            handler._complete(
                ProcessingMessage(dataset_id="ds-1", version=1),
                metrics_dict={"review_count": 0, "pages_captured": 0, "extraction": {}},
                review_count=0,
                page_details=[],
                duration_ms=1,
                error=None,
            )
        decision = spy.calls[0]["decision"]
        assert decision.outcome is Outcome.FAILED
        assert decision.message == completion_mod.ZERO_REVIEWS_MESSAGE


class TestFinalRetryFailure:
    """Final-attempt errors fail the version; non-final errors propagate.

    _Validates: Requirements 7.2, 7.4_
    """

    def _handler_failing_in_stages(self, error: Exception) -> ProcessingHandler:
        handler = ProcessingHandler()
        handler._start_guard = lambda message: GuardDecision.CONTINUE  # type: ignore[method-assign]

        def _boom(message: ProcessingMessage, budget: TimeBudget) -> None:
            raise error

        handler._run_stages = _boom  # type: ignore[method-assign]
        return handler

    def test_non_final_attempt_propagates(self) -> None:
        handler = self._handler_failing_in_stages(RuntimeError("boom"))
        completed: list[Any] = []
        handler._fail_final = lambda *a, **k: completed.append(a)  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="boom"):
            handler._run_pipeline(
                ProcessingMessage(dataset_id="ds-1", version=1),
                _meta(receive_count=1),  # attempt 1/3, not final
                TimeBudget.start(),
            )
        # The version is not failed on a non-final attempt; SQS retries.
        assert completed == []

    def test_final_attempt_fails_version_with_generic_message(self) -> None:
        handler = self._handler_failing_in_stages(RuntimeError("secret internals"))
        spy = _CompleteSpy()
        with patch("app.handlers.processing.completion.complete", spy):
            handler._run_pipeline(
                ProcessingMessage(dataset_id="ds-1", version=1),
                _meta(receive_count=3),  # final attempt
                TimeBudget.start(),
            )
        decision = spy.calls[0]["decision"]
        assert decision.outcome is Outcome.FAILED
        assert decision.message == completion_mod.GENERIC_FAILURE_MESSAGE
        assert "secret internals" not in decision.message

    def test_final_attempt_ai_unavailable_uses_ai_message(self) -> None:
        handler = self._handler_failing_in_stages(AIUnavailable("provider down"))
        spy = _CompleteSpy()
        with patch("app.handlers.processing.completion.complete", spy):
            handler._run_pipeline(
                ProcessingMessage(dataset_id="ds-1", version=2),  # a refresh
                _meta(receive_count=3),
                TimeBudget.start(),
            )
        decision = spy.calls[0]["decision"]
        assert decision.outcome is Outcome.FAILED
        # AI message + the refresh "previous reviews kept" note (Req 6.5/7.4).
        assert decision.message == (
            completion_mod.AI_UNAVAILABLE_MESSAGE + completion_mod.REFRESH_KEPT_SUFFIX
        )
        # A failed refresh records zero reviews and does not advance active.
        assert spy.calls[0]["review_count"] == 0
        assert decision.set_active is False
