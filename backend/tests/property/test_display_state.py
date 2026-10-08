"""Property-based test for app.datasets.library.derive_display_state (dataset-library task 8).

Property 1: Display state is total and correct.
  For any combination of status and active version, ``display_state`` SHALL
  match the table in the design.
  Validates: Requirement 2.5

This drives the real :func:`derive_display_state` over the whole input space —
every ``DatasetStatus`` (and a few out-of-vocabulary string statuses, since the
function is documented to stay total for any input) crossed with "has an active
version" vs "no active version". The property asserts two things:

1. **Total** — the function returns one of the five known ``display_state``
   values for every input, never raising and never returning anything else.
2. **Correct** — the returned value equals the design's table, re-stated here
   independently so the test fails if either the table or the implementation
   drifts.

No AI, no S3, no DB: ``derive_display_state`` is pure logic.
"""

from __future__ import annotations

from app.datasets.library import DisplayState, derive_display_state
from app.db.models import DatasetStatus
from hypothesis import given
from hypothesis import strategies as st

# The five display states the design defines (the function's full range).
_VALID_DISPLAY_STATES: frozenset[str] = frozenset(
    ("processing", "ready_refreshing", "ready", "ready_refresh_failed", "failed")
)

# Every real status value, plus a couple of out-of-vocabulary strings. The
# function documents that it stays total even for an unknown status, so the
# generator deliberately includes values the DB enum would never store.
_STATUS = st.one_of(
    st.sampled_from([s.value for s in DatasetStatus]),
    st.sampled_from(["", "unknown", "RETIRED"]),
)

# "has an active version" vs "none"; the specific integer never matters, only
# whether it is None, so a fixed non-None value exercises the "set" branch.
_ACTIVE_VERSION = st.one_of(st.none(), st.integers(min_value=1, max_value=99))


def _expected(status: str, active_version: int | None) -> DisplayState:
    """The design's ``display_state`` table, re-stated independently.

    This mirrors the design table (and the documented fallback for statuses
    outside the enum) without reusing the implementation's branching, so the
    test is a genuine oracle rather than a copy of the code under test.

    =============================  ===============  ======================
    status                         active_version   display_state
    =============================  ===============  ======================
    ``requested``/``processing``   null             ``processing``
    ``requested``/``processing``   set              ``ready_refreshing``
    ``updated``                    set              ``ready``
    ``updated``                    null             ``processing`` (safe)
    ``failed``                     set              ``ready_refresh_failed``
    ``failed``                     null             ``failed``
    other                          set              ``ready`` (safe)
    other                          null             ``processing`` (safe)
    =============================  ===============  ======================
    """
    has_active = active_version is not None
    if status in (DatasetStatus.REQUESTED.value, DatasetStatus.PROCESSING.value):
        return "ready_refreshing" if has_active else "processing"
    if status == DatasetStatus.UPDATED.value:
        return "ready" if has_active else "processing"
    if status == DatasetStatus.FAILED.value:
        return "ready_refresh_failed" if has_active else "failed"
    return "ready" if has_active else "processing"


@given(status=_STATUS, active_version=_ACTIVE_VERSION)
def test_display_state_is_total_and_correct(status: str, active_version: int | None) -> None:
    """Property 1: Display state is total and correct.

    For any (status, active_version), derive_display_state returns exactly the
    design table's value, and always one of the five known display states.
    Validates: Requirement 2.5
    """
    result = derive_display_state(status, active_version)

    # Total: always a known display_state, never an error.
    assert result in _VALID_DISPLAY_STATES

    # Correct: matches the design table (independent oracle).
    assert result == _expected(status, active_version)


@given(status=st.sampled_from(list(DatasetStatus)), active_version=_ACTIVE_VERSION)
def test_display_state_accepts_enum_members(
    status: DatasetStatus, active_version: int | None
) -> None:
    """Property 1: Display state is total and correct (enum inputs).

    ``derive_display_state`` accepts a ``DatasetStatus`` member as well as its
    string value, and both produce the same, table-matching result.
    Validates: Requirement 2.5
    """
    from_enum = derive_display_state(status, active_version)
    from_value = derive_display_state(status.value, active_version)

    assert from_enum == from_value
    assert from_enum == _expected(status.value, active_version)
    assert from_enum in _VALID_DISPLAY_STATES
