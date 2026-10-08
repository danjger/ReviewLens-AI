"""Unit tests for app.handlers.completion (review-analysis task 6).

Task 6 ends a dataset version: on success it moves the dataset to ``updated``,
advances ``active_version``, writes the ``metrics`` column, completes the
``dataset_versions`` row, and records the actual result next to the viability
prediction; on zero reviews it moves to ``failed`` leaving ``active_version``
and ``metrics`` unchanged; on a final-retry failure it moves to ``failed`` with
a user-safe message (the AI-unavailable message for an AI outage).

These tests split the two halves of the stage per the design:

- :func:`decide_completion` is a pure function — every branch (success, zero
  reviews, final-retry generic, final-retry AI-unavailable, and the refresh
  note) is unit-tested without a database.
- :func:`complete` is the DB wiring — tested against a stub session that records
  the SQL it was handed, so the conditional claim, the success-only
  active_version/metrics write, the ``viability.actual`` write, and the single
  post-claim ``transition`` are all asserted without PostgreSQL. The idempotent
  no-op (a redelivery whose claim loses) is asserted too.

_Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 7.2, 7.4, 8.1_
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from app.db.models import DatasetStatus
from app.extraction.errors import AIUnavailable
from app.handlers.completion import (
    AI_UNAVAILABLE_MESSAGE,
    GENERIC_FAILURE_MESSAGE,
    REFRESH_KEPT_SUFFIX,
    ZERO_REVIEWS_MESSAGE,
    ActualResult,
    CompletionDecision,
    Outcome,
    complete,
    decide_completion,
)

# ---------------------------------------------------------------------------
# decide_completion — the pure decision (every branch)
# ---------------------------------------------------------------------------


class TestDecideCompletionSuccess:
    """A run with ≥ 1 review ends ``updated`` and advances active/metrics.

    _Validates: Requirement 6.1_
    """

    def test_one_review_is_updated(self) -> None:
        d = decide_completion(review_count=1, error=None, is_refresh=False)
        assert d.outcome is Outcome.UPDATED
        assert d.status is DatasetStatus.UPDATED
        assert d.message == "updated"
        assert d.set_active is True
        assert d.write_metrics is True

    def test_many_reviews_is_updated(self) -> None:
        d = decide_completion(review_count=212, error=None, is_refresh=True)
        # A *successful* refresh still advances active_version and writes metrics.
        assert d.outcome is Outcome.UPDATED
        assert d.set_active is True
        assert d.write_metrics is True
        assert d.message == "updated"


class TestDecideCompletionZeroReviews:
    """Zero reviews ends ``failed`` with the fixed message, no active/metrics.

    _Validates: Requirements 6.2, 6.5_
    """

    def test_zero_reviews_fails_with_message(self) -> None:
        d = decide_completion(review_count=0, error=None, is_refresh=False)
        assert d.outcome is Outcome.FAILED
        assert d.status is DatasetStatus.FAILED
        assert d.message == ZERO_REVIEWS_MESSAGE
        assert d.set_active is False
        assert d.write_metrics is False

    def test_zero_reviews_on_refresh_notes_previous_kept(self) -> None:
        d = decide_completion(review_count=0, error=None, is_refresh=True)
        assert d.outcome is Outcome.FAILED
        assert d.message == ZERO_REVIEWS_MESSAGE + REFRESH_KEPT_SUFFIX
        # A failed refresh never advances active_version or overwrites metrics.
        assert d.set_active is False
        assert d.write_metrics is False

    def test_negative_count_is_treated_as_zero(self) -> None:
        # Defensive: a count that is somehow negative still fails safely.
        d = decide_completion(review_count=-1, error=None, is_refresh=False)
        assert d.outcome is Outcome.FAILED


class TestDecideCompletionFinalRetryFailure:
    """A final-attempt error ends ``failed`` with a user-safe message.

    _Validates: Requirements 7.2, 7.4, 6.5_
    """

    def test_generic_error_uses_generic_message(self) -> None:
        d = decide_completion(review_count=0, error=RuntimeError("boom"), is_refresh=False)
        assert d.outcome is Outcome.FAILED
        assert d.message == GENERIC_FAILURE_MESSAGE
        # The message must not leak internals.
        assert "boom" not in d.message
        assert d.set_active is False
        assert d.write_metrics is False

    def test_ai_unavailable_uses_ai_message(self) -> None:
        d = decide_completion(
            review_count=0,
            error=AIUnavailable("provider down"),
            is_refresh=False,
            ai_unavailable=True,
        )
        assert d.outcome is Outcome.FAILED
        assert d.message == AI_UNAVAILABLE_MESSAGE

    def test_error_takes_precedence_over_review_count(self) -> None:
        # Even if some reviews existed, a caught final-attempt error fails the
        # version (the metrics/reviews for this attempt are not trusted).
        d = decide_completion(review_count=50, error=RuntimeError("x"), is_refresh=False)
        assert d.outcome is Outcome.FAILED
        assert d.set_active is False

    def test_failed_refresh_notes_previous_kept(self) -> None:
        d = decide_completion(
            review_count=0,
            error=AIUnavailable("down"),
            is_refresh=True,
            ai_unavailable=True,
        )
        assert d.message == AI_UNAVAILABLE_MESSAGE + REFRESH_KEPT_SUFFIX


# ---------------------------------------------------------------------------
# ActualResult
# ---------------------------------------------------------------------------


class TestActualResult:
    """The actual-vs-predicted block written under status_detail.viability.

    _Validates: Requirement 6.4_
    """

    def test_as_dict_has_the_four_fields(self) -> None:
        actual = ActualResult(reviews=212, pages=10, method="selectors", fallbacks=1)
        assert actual.as_dict() == {
            "reviews": 212,
            "pages": 10,
            "method": "selectors",
            "fallbacks": 1,
        }


# ---------------------------------------------------------------------------
# complete() — DB wiring against a stub session
# ---------------------------------------------------------------------------


class _StubResult:
    def __init__(self, first: Any) -> None:
        self._first = first

    def first(self) -> Any:
        return self._first


class _StubSession:
    """Records the statements complete() runs and serves canned claim results.

    The dataset lock (``SELECT 1 ... FOR UPDATE``) returns a row unless
    ``dataset_exists`` is False; the ``UPDATE dataset_versions ... RETURNING``
    returns a row unless ``claim_won`` is False (a lost claim → idempotent
    no-op). Every executed statement is captured in ``statements`` so tests can
    assert what was (and wasn't) written.
    """

    def __init__(self, *, dataset_exists: bool = True, claim_won: bool = True) -> None:
        self._dataset_exists = dataset_exists
        self._claim_won = claim_won
        self.statements: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(statement).split())
        bound = dict(getattr(statement, "_bindparams", {}) or {})
        recorded: dict[str, Any] = {k: getattr(v, "value", v) for k, v in bound.items()}
        if params:
            recorded.update(params)
        self.statements.append((sql, recorded))

        if "SELECT 1 FROM datasets" in sql:
            return _StubResult((1,) if self._dataset_exists else None)
        if "UPDATE dataset_versions" in sql:
            return _StubResult((1,) if self._claim_won else None)
        # The active_version/metrics and viability.actual updates return nothing.
        return _StubResult(None)


@contextmanager
def _stub_scope(session: _StubSession) -> Iterator[_StubSession]:
    yield session


def _metrics(method: str = "selectors") -> dict[str, Any]:
    return {
        "review_count": 3,
        "pages_captured": 2,
        "extraction": {"method": method, "pages_by_selectors": 2, "pages_by_ai": 0},
    }


def _run_complete(
    *,
    decision: CompletionDecision,
    session: _StubSession,
    metrics: dict[str, Any] | None = None,
    actual: ActualResult | None = None,
    review_count: int = 3,
    version: int = 1,
) -> tuple[bool, list[tuple[Any, ...]]]:
    """Call complete() with the session and transition patched; return
    (won, transition_calls)."""
    calls: list[tuple[Any, ...]] = []

    def _transition(dataset_id: str, status: Any, message: str, extra: Any = None) -> None:
        calls.append((dataset_id, status, message, extra))

    with (
        patch("app.handlers.completion.session_scope", lambda: _stub_scope(session)),
        patch("app.handlers.completion.transition", _transition),
    ):
        won = complete(
            dataset_id="ds-1",
            version=version,
            decision=decision,
            metrics=metrics if metrics is not None else _metrics(),
            actual=actual
            or ActualResult(reviews=review_count, pages=2, method="selectors", fallbacks=0),
            review_count=review_count,
            duration_ms=1234,
        )
    return won, calls


class TestCompleteSuccess:
    """A successful completion claims the row, advances active/metrics, and
    transitions once.

    _Validates: Requirements 6.1, 6.3, 6.4, 8.1_
    """

    def test_claims_version_and_transitions_to_updated(self) -> None:
        decision = decide_completion(review_count=3, error=None, is_refresh=False)
        session = _StubSession()
        won, calls = _run_complete(decision=decision, session=session)

        assert won is True
        # Exactly one transition, to updated, carrying the version/count/duration.
        assert len(calls) == 1
        dataset_id, status, message, extra = calls[0]
        assert dataset_id == "ds-1"
        assert status is DatasetStatus.UPDATED
        assert message == "updated"
        assert extra == {"version": 1, "review_count": 3, "duration_ms": 1234}

    def test_completes_version_row_with_outcome_and_method(self) -> None:
        decision = decide_completion(review_count=3, error=None, is_refresh=False)
        session = _StubSession()
        _run_complete(decision=decision, session=session)

        version_updates = [s for s in session.statements if "UPDATE dataset_versions" in s[0]]
        assert len(version_updates) == 1
        sql, params = version_updates[0]
        # Idempotent claim: only completes while still in flight.
        assert "outcome IS NULL" in sql
        assert params["outcome"] == "updated"
        assert params["extraction_method"] == "selectors"
        assert params["review_count"] == 3

    def test_advances_active_version_and_writes_metrics(self) -> None:
        decision = decide_completion(review_count=3, error=None, is_refresh=False)
        session = _StubSession()
        _run_complete(decision=decision, session=session, version=2)

        active_updates = [
            s for s in session.statements if "active_version" in s[0] and "metrics" in s[0]
        ]
        assert len(active_updates) == 1
        sql, params = active_updates[0]
        # Monotonic advance (GREATEST) so an older re-run never pulls it back.
        assert "GREATEST" in sql
        assert params["version"] == 2
        # The metrics column is written with the computed metrics dict.
        assert json.loads(params["metrics"])["review_count"] == 3

    def test_records_viability_actual(self) -> None:
        decision = decide_completion(review_count=3, error=None, is_refresh=False)
        session = _StubSession()
        actual = ActualResult(reviews=3, pages=2, method="selectors", fallbacks=1)
        _run_complete(decision=decision, session=session, actual=actual)

        viability_updates = [s for s in session.statements if "viability,actual" in s[0]]
        assert len(viability_updates) == 1
        _, params = viability_updates[0]
        assert params["actual"] == {
            "reviews": 3,
            "pages": 2,
            "method": "selectors",
            "fallbacks": 1,
        }


class TestCompleteFailure:
    """A failed completion leaves active_version/metrics untouched.

    _Validates: Requirements 6.2, 6.5_
    """

    def test_zero_reviews_does_not_write_active_or_metrics(self) -> None:
        decision = decide_completion(review_count=0, error=None, is_refresh=True)
        session = _StubSession()
        won, calls = _run_complete(
            decision=decision, session=session, metrics={}, review_count=0, version=2
        )

        assert won is True
        # No active_version/metrics write on a failed version.
        assert not any("active_version" in s[0] for s in session.statements)
        # Version row completed with outcome=failed.
        version_updates = [s for s in session.statements if "UPDATE dataset_versions" in s[0]]
        assert version_updates[0][1]["outcome"] == "failed"
        # One failed transition, with the refresh-kept message.
        assert len(calls) == 1
        _, status, message, _ = calls[0]
        assert status is DatasetStatus.FAILED
        assert message == ZERO_REVIEWS_MESSAGE + REFRESH_KEPT_SUFFIX

    def test_viability_actual_recorded_even_on_failure(self) -> None:
        # Requirement 6.4: the actual result is recorded on *every* run.
        decision = decide_completion(review_count=0, error=None, is_refresh=False)
        session = _StubSession()
        _run_complete(decision=decision, session=session, metrics={}, review_count=0)
        assert any("viability,actual" in s[0] for s in session.statements)


class TestCompleteIdempotency:
    """A lost claim is an idempotent no-op (Property 5 wiring).

    _Validates: Requirement 7.3_
    """

    def test_lost_claim_makes_no_transition(self) -> None:
        decision = decide_completion(review_count=3, error=None, is_refresh=False)
        session = _StubSession(claim_won=False)
        won, calls = _run_complete(decision=decision, session=session)

        assert won is False
        # No transition and no active_version/viability writes when the claim is
        # already taken by another writer.
        assert calls == []
        assert not any("active_version" in s[0] for s in session.statements)
        assert not any("viability,actual" in s[0] for s in session.statements)

    def test_missing_dataset_raises(self) -> None:
        import pytest

        decision = decide_completion(review_count=3, error=None, is_refresh=False)
        session = _StubSession(dataset_exists=False)
        with pytest.raises(ValueError, match="does not exist"):
            _run_complete(decision=decision, session=session)
