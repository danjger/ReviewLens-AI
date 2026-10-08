"""Property-based test for the review-analysis completion stage (task 6).

Property 4: Active version only moves forward on success.
  For any sequence of successful and failed versions, ``active_version`` SHALL
  equal the highest version whose outcome is ``updated``.
  Validates: Requirements 6.1, 6.5

The property is tested against the real production rule,
:func:`app.handlers.completion.next_active_version`, which is the exact rule the
``active_version`` SQL in :func:`app.handlers.completion.complete` applies
(``GREATEST(COALESCE(active_version, 0), version)`` on success; unchanged on
failure). Hypothesis generates arbitrary sequences of version completions — each
either a success (≥ 1 review) or a failure (zero reviews or a final-attempt
error), in any order, including replays and out-of-order versions — and folds
them through ``next_active_version`` the way the pipeline would across runs. The
result is compared against an independent oracle: the highest version whose
outcome was ``updated``.

``next_active_version`` is pure, so no database or stubbing is needed; the DB
wiring that applies it (the conditional claim + the monotonic SQL) is covered by
the completion unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.handlers.completion import decide_completion, next_active_version
from hypothesis import given, settings
from hypothesis import strategies as st


@dataclass(frozen=True)
class Completion:
    """One version completing in a generated sequence.

    Attributes:
        version: The data version that completed.
        succeeded: Whether it finished ``updated`` (≥ 1 review, no error).
    """

    version: int
    succeeded: bool


def _completions() -> st.SearchStrategy[list[Completion]]:
    """Sequences of version completions, successes and failures interleaved.

    Versions are drawn from a small range so replays and out-of-order
    completions (an older version finishing after a newer one) occur often,
    which is exactly where a non-monotonic ``active_version`` would regress.
    """
    return st.lists(
        st.builds(
            Completion,
            version=st.integers(min_value=1, max_value=6),
            succeeded=st.booleans(),
        ),
        max_size=20,
    )


def _oracle(completions: list[Completion]) -> int | None:
    """The highest version whose outcome was ``updated`` (independent of impl)."""
    succeeded = [c.version for c in completions if c.succeeded]
    return max(succeeded) if succeeded else None


@settings(max_examples=300)
@given(completions=_completions())
def test_active_version_is_highest_successful(completions: list[Completion]) -> None:
    """Property 4: Active version only moves forward on success.

    Folding any sequence of successful and failed completions through the
    production rule leaves ``active_version`` equal to the highest version that
    finished ``updated`` (or ``None`` if none did).
    Validates: Requirements 6.1, 6.5
    """
    active: int | None = None
    for completion in completions:
        # The pipeline builds the decision from the run's facts; a success is
        # review_count >= 1 with no error, a failure is zero reviews.
        decision = decide_completion(
            review_count=1 if completion.succeeded else 0,
            error=None,
            is_refresh=completion.version >= 2,
        )
        active = next_active_version(active, completion.version, decision)

    assert active == _oracle(completions)


@settings(max_examples=200)
@given(completions=_completions())
def test_active_version_never_decreases(completions: list[Completion]) -> None:
    """Property 4 (corollary): ``active_version`` is monotonic non-decreasing.

    No completion — success or failure, in or out of order — ever lowers
    ``active_version``. A failed refresh in particular leaves it untouched, so
    the last good version keeps serving (Requirement 6.5).
    Validates: Requirements 6.1, 6.5
    """
    active: int | None = None
    for completion in completions:
        decision = decide_completion(
            review_count=1 if completion.succeeded else 0,
            error=None,
            is_refresh=completion.version >= 2,
        )
        updated = next_active_version(active, completion.version, decision)
        # Never moves backward.
        assert (updated or 0) >= (active or 0)
        # A failed version leaves it exactly unchanged.
        if not completion.succeeded:
            assert updated == active
        active = updated
