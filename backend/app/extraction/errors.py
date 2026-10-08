"""Error types raised by the Extraction Engine.

Both errors are transient: callers (the check and processing handlers) should
let them propagate so the message is redelivered rather than treated as a
permanent failure.  They subclass the shared
:class:`app.core.errors.RetryableError` base so callers can catch all
retryable failures uniformly, while the distinct classes let them tell an
AI-provider outage apart from a Locator that could not produce a valid
response.
"""

from __future__ import annotations

from app.core.errors import RetryableError


class AIUnavailable(RetryableError):
    """The AI provider is unavailable or the global AI limit was reached.

    Raised when a Claude call fails to complete (provider error, timeout) or
    the instrumented client refuses the call because the global AI rate limit
    is exhausted.  Distinct from :class:`LocatorUnavailable`, which signals a
    response that completed but could not be made schema-valid.
    """

    code = "AI_UNAVAILABLE"


class LocatorUnavailable(RetryableError):
    """The Review Locator could not produce a schema-valid response.

    Raised after the Locator's response fails schema validation and the single
    repair retry also fails.
    """

    code = "LOCATOR_UNAVAILABLE"
