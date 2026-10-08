"""Versioned prompt loading for the guardrailed chat (Task 1.1).

The chat's prompts live as versioned Markdown files under
``backend/app/chat/prompts/`` (per the ``guardrailed-chat`` design). Each file
is named ``{name}_v{n}.md`` and its stem is the *version string* recorded on the
saved Exchange (``prompt_version`` — e.g. ``"system_v1"``) so an answer can be
traced back to the exact prompt that produced it.

This mirrors the per-module loaders already used by the extraction and worker
tasks (``app.extraction.locator.load_prompt`` and
``app.worker.ai._repair.load_repair_prompt``): a small, memoized file read keyed
on the version, resolved relative to this module so it works regardless of the
process CWD. It adds *version tracking* on top — :func:`load_prompt` returns the
text paired with its version string so callers never have to repeat the literal.

Changing a prompt file requires a new version (``system_v2.md``) and running the
matching evaluation (steering: changing a prompt requires ``make eval``). Bumping
:data:`SYSTEM_PROMPT_VERSION` keeps the default in step.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

#: The current system-prompt version. Recorded on each saved Exchange as
#: ``prompt_version`` so an answer traces back to the exact prompt. Bump by
#: adding ``system_v2.md`` and running the scope-guard evaluation.
SYSTEM_PROMPT_VERSION = "system_v1"

#: The current scope pre-check prompt version (Task 2). Recorded alongside the
#: pre-check result; bump by adding ``precheck_v2.md`` and running the
#: scope-guard evaluation. Loaded through the same :func:`load_prompt` seam.
PRECHECK_PROMPT_VERSION = "precheck_v1"

#: Directory holding the chat's versioned prompt files
#: (``backend/app/chat/prompts/``). Resolved relative to this module so the
#: files load the same way under Lambda, Compose, and the test runner.
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True, slots=True)
class LoadedPrompt:
    """A prompt file's text together with the version it was loaded from.

    :ivar text: The raw prompt text, with template placeholders intact
        (``{entity.name}``, ``{entity.category}``, ``{platform}``,
        ``{original_url}``). Filling placeholders is the caller's job during
        message assembly (Task 1.3).
    :ivar version: The version string (the file stem, e.g. ``"system_v1"``),
        suitable for the Exchange's ``prompt_version`` field.
    """

    text: str
    version: str


@functools.lru_cache(maxsize=4)
def _read_prompt_text(version: str) -> str:
    """Read ``{version}.md`` from the chat prompts directory.

    Memoized because prompts are static files read on every chat request.
    Raises :class:`FileNotFoundError` if the version does not exist, so a typo
    fails loudly rather than silently sending an empty system prompt.
    """
    return (_PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")


def load_prompt(version: str = SYSTEM_PROMPT_VERSION) -> LoadedPrompt:
    """Return a chat prompt's text paired with its version string.

    :param version: The prompt version (file stem), e.g. ``"system_v1"``.
        Defaults to :data:`SYSTEM_PROMPT_VERSION`.
    :returns: A :class:`LoadedPrompt` carrying the text and the version, so the
        caller records ``prompt_version`` without repeating the literal.
    :raises FileNotFoundError: If no file exists for ``version``.
    """
    return LoadedPrompt(text=_read_prompt_text(version), version=version)


def load_system_prompt() -> LoadedPrompt:
    """Return the current system prompt and its version (convenience wrapper)."""
    return load_prompt(SYSTEM_PROMPT_VERSION)


def load_precheck_prompt() -> LoadedPrompt:
    """Return the current scope pre-check prompt and its version (Task 2)."""
    return load_prompt(PRECHECK_PROMPT_VERSION)
