"""Property-based test for the review-analysis start guard (task 1.2).

Property 6: Start guard.
  For any combination of message version, dataset status, data version,
  completion state, and archive flag, the guard SHALL proceed exactly in the
  cases listed in Requirement 1.2.
  Validates: Requirement 1.2

Requirement 1.2 (and 1.1) in full:
  - WHEN a message is received for a dataset with status `requested`, the Worker
    SHALL start processing that version (START).
  - WHEN a message is a retry of a version already `processing`, the Worker
    SHALL continue that version (CONTINUE).
  - IF the message's version is older than the dataset's current data version,
    that version is already completed, or the dataset is archived, THEN the
    Worker SHALL skip the message without error (SKIP).

This test sweeps the whole input space with Hypothesis and compares
``decide_start`` against an **independent** oracle derived straight from the
requirement text, so the implementation and the oracle can't share a mistake.
``decide_start`` is a pure function of :class:`DatasetState`, so no database or
stubbing is needed here; the DB wiring is covered by the unit tests.
"""

from __future__ import annotations

from app.db.models import DatasetStatus
from app.handlers.processing import DatasetState, GuardDecision, decide_start
from hypothesis import given, settings
from hypothesis import strategies as st


def _oracle(message_version: int, state: DatasetState) -> GuardDecision:
    """Requirement 1.2 restated independently of the implementation.

    Skip conditions (superseded / already completed / archived) are evaluated
    first because the requirement states them as unconditional skips. A message
    newer than the row's ``data_version`` has no landed version to act on, so it
    is also a skip. Only on the current, live version does status decide:
    ``requested`` → START, ``processing`` → CONTINUE, anything else → SKIP.
    """
    if message_version < state.data_version:
        return GuardDecision.SKIP
    if state.version_completed:
        return GuardDecision.SKIP
    if state.archived:
        return GuardDecision.SKIP
    if message_version > state.data_version:
        return GuardDecision.SKIP
    if state.status is DatasetStatus.REQUESTED:
        return GuardDecision.START
    if state.status is DatasetStatus.PROCESSING:
        return GuardDecision.CONTINUE
    return GuardDecision.SKIP


@settings(max_examples=300)
@given(
    message_version=st.integers(min_value=1, max_value=10),
    status=st.sampled_from(list(DatasetStatus)),
    data_version=st.integers(min_value=0, max_value=10),
    version_completed=st.booleans(),
    archived=st.booleans(),
)
def test_start_guard_matches_requirement(
    message_version: int,
    status: DatasetStatus,
    data_version: int,
    version_completed: bool,
    archived: bool,
) -> None:
    """Property 6: Start guard.

    For any combination of message version, dataset status, data version,
    completion state, and archive flag, the guard proceeds exactly in the cases
    listed in Requirement 1.2.
    Validates: Requirement 1.2
    """
    state = DatasetState(
        status=status,
        data_version=data_version,
        version_completed=version_completed,
        archived=archived,
    )

    assert decide_start(message_version, state) is _oracle(message_version, state)


@settings(max_examples=200)
@given(
    message_version=st.integers(min_value=1, max_value=10),
    status=st.sampled_from(list(DatasetStatus)),
    data_version=st.integers(min_value=0, max_value=10),
    version_completed=st.booleans(),
    archived=st.booleans(),
)
def test_skip_conditions_always_skip(
    message_version: int,
    status: DatasetStatus,
    data_version: int,
    version_completed: bool,
    archived: bool,
) -> None:
    """Property 6: the three skip conditions always skip, whatever the status.

    A superseded version, an already-completed version, or an archived dataset
    is never processed — the status is irrelevant.
    Validates: Requirement 1.2
    """
    state = DatasetState(
        status=status,
        data_version=data_version,
        version_completed=version_completed,
        archived=archived,
    )
    is_skip_condition = message_version < data_version or version_completed or archived
    if is_skip_condition:
        assert decide_start(message_version, state) is GuardDecision.SKIP
