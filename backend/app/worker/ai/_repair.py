"""Shared JSON schema-repair retry for the post-extraction AI tasks (task 4.4).

The three AI tasks in ``worker/ai/`` — :mod:`app.worker.ai.profile`,
:mod:`app.worker.ai.sentiment`, and :mod:`app.worker.ai.themes` — were each
built with the same seam: a single ``_create_message(system, user)`` call site
that raises :class:`~app.extraction.errors.AIUnavailable` on a provider outage
or the global AI limit, and a ``_parse_*`` function that raises that task's
``XInvalidError`` when the model's output cannot be made schema-valid. Each task
then applies its own fallback (profile → low-confidence title; sentiment →
rating-based; themes → omit with a warning).

The design's Error Handling says: *"AI output that fails JSON or schema
validation is retried once with a repair prompt. If it fails again,"* the task's
fallback applies. This module factors that single-retry **call → parse → on
invalid, repair-call → parse** skeleton out of the three tasks so the retry is
written and tested once. The fallback stays in each task because it differs per
task; this helper only decides *whether a valid parse was obtained within one
repair attempt* and, if not, re-raises the task's invalid error for the caller
to turn into its fallback.

Two guarantees this helper is responsible for (Requirements 7.1, 7.4):

* **A schema/parse failure triggers exactly one repair retry.** The first
  response is parsed; if that raises the task's invalid error, one more model
  call is made with a repair prompt that tells the model what was wrong, and its
  response is parsed. There is never more than one repair attempt.
* **``AIUnavailable`` propagates immediately and is never treated as a schema
  failure.** Only the task's ``invalid_error`` type is caught around the parse;
  :class:`AIUnavailable` raised by ``create`` (provider down / global limit)
  flies straight out of this helper on either the first or the repair call, so
  the pipeline lets SQS retry with backoff. The repair path is *not* a catch-all.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

#: Directory holding versioned prompt files (``backend/prompts/``). Resolved
#: relative to this module so it works regardless of the process CWD, matching
#: the per-task prompt loaders.
_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"

#: The repair-prompt version appended to the user message on the retry. It is a
#: short, generic "your previous output was invalid, here is why, return output
#: matching the tool's schema" instruction shared by all three tasks, kept as a
#: versioned file under ``prompts/`` per steering. Bump by adding
#: ``repair_v2.md`` and running the matching evaluation.
REPAIR_PROMPT_VERSION = "repair_v1"


@functools.lru_cache(maxsize=2)
def load_repair_prompt(version: str = REPAIR_PROMPT_VERSION) -> str:
    """Return the text of the versioned repair-prompt template.

    Reads ``backend/prompts/{version}.md``. The template contains a single
    ``{reason}`` placeholder filled with the human-readable validation detail
    from the task's invalid error. Memoized because it is a static file read on
    every repair. Raises :class:`FileNotFoundError` if the version is missing,
    so a typo fails loudly.
    """
    return (_PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")


def build_repair_user(original_user: str, reason: str) -> str:
    """Append the repair instruction (with *reason*) to the original user message.

    The repair call resends the original request so the model still has the
    reviews/content as context, then appends the versioned repair instruction
    naming what was wrong (*reason* — the task's invalid-error message) and
    asking for output that matches the tool's schema. This is the only place the
    repair prompt is assembled, so all three tasks phrase the retry identically.
    """
    return original_user + "\n\n" + load_repair_prompt().format(reason=reason)


def call_parse_repair[T](
    *,
    create: Callable[[str, str], Any],
    parse: Callable[[Any], T],
    invalid_error: type[Exception],
    system: str,
    user: str,
) -> T:
    """Call, parse, and on a schema failure retry once with a repair prompt.

    This is the shared body of task 4.4's repair retry. It:

    1. Calls ``create(system, user)`` and parses the response with ``parse``.
    2. If ``parse`` raises ``invalid_error`` (a JSON/schema failure), makes **one**
       more ``create`` call whose user message is *user* plus a repair
       instruction naming the failure reason, and parses that response.
    3. Returns the parsed result from whichever call first produced one.

    It deliberately does **not** apply any fallback: if the repaired response
    *also* raises ``invalid_error``, that error propagates so the caller can
    apply its own task-specific fallback at the existing catch site. This keeps
    the three different fallback behaviours in their own modules.

    :param create: The task's ``_create_message``; takes ``(system, user)`` and
        either returns the SDK message or raises
        :class:`~app.extraction.errors.AIUnavailable`.
    :param parse: The task's ``_parse_*``; returns the validated result or raises
        ``invalid_error``.
    :param invalid_error: The task's schema-failure exception type
        (``ProfileInvalidError`` / ``SentimentInvalidError`` /
        ``ThemesInvalidError``). **Only** this type is caught; ``AIUnavailable``
        is never caught here and propagates immediately (Requirement 7.4).
    :param system: The system prompt (unchanged between the two calls).
    :param user: The original user message; the repair call appends to it.
    :returns: The validated parse result.
    :raises invalid_error: The repair response also failed schema validation.
    :raises AIUnavailable: Either model call hit a provider outage or the global
        AI limit — propagated untouched, never turned into a repair retry.
    """
    response = create(system, user)
    try:
        return parse(response)
    except invalid_error as first_failure:
        # Schema/parse failure (NOT AIUnavailable — that is not caught and has
        # already propagated). Retry exactly once with a repair prompt that
        # tells the model what was wrong.
        repair_user = build_repair_user(user, str(first_failure))
        repaired = create(system, repair_user)
        return parse(repaired)
