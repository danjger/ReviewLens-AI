"""Per-invocation wall-clock budget shared across pipeline stages.

Extracted from :mod:`app.handlers.processing` (review-analysis task 1.1) so the
collection stage (task 2) and later stages can consult the same budget without
importing the handler module (which would create an import cycle). The handler
re-exports :class:`TimeBudget` and :data:`TIME_BUDGET_SECONDS` for backwards
compatibility, so existing callers and tests are unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

#: Wall-clock budget for one processing message, in seconds (design: "Lambda
#: timeout is 15 minutes"). Page collection stops well before this (task 2) so
#: later stages still have time to run and the version can be completed before
#: the Lambda is killed or the SQS visibility window lapses.
TIME_BUDGET_SECONDS = 15 * 60


@dataclass(frozen=True)
class TimeBudget:
    """A monotonic deadline for one processing invocation.

    Derived fresh from :func:`time.monotonic` for every message so no state
    leaks between invocations (stateless rule). Later stages consult
    :meth:`remaining` / :meth:`expired` to decide whether to keep collecting
    pages or to stop early and continue with what has been gathered (design:
    "If collecting pages approaches 12 minutes, stop collecting …"). A monotonic
    clock is used so the budget is immune to wall-clock adjustments.
    """

    started_monotonic: float
    budget_seconds: float = TIME_BUDGET_SECONDS

    @classmethod
    def start(cls, budget_seconds: float = TIME_BUDGET_SECONDS) -> TimeBudget:
        """Begin a budget now."""
        return cls(started_monotonic=time.monotonic(), budget_seconds=budget_seconds)

    def elapsed(self) -> float:
        """Seconds elapsed since the budget started."""
        return time.monotonic() - self.started_monotonic

    def remaining(self) -> float:
        """Seconds left before the budget is exhausted (never negative)."""
        return max(0.0, self.budget_seconds - self.elapsed())

    def expired(self) -> bool:
        """True once the budget has been fully consumed."""
        return self.remaining() <= 0.0
