"""Unit tests for the chat prompt loader (``app.chat.prompts``), Task 1.1.

Covers:

- The system prompt loads from the versioned file and carries its version
  string (version tracking for the Exchange's ``prompt_version``).
- The prompt contains the SCOPE, EVIDENCE, and SECURITY clauses, the three
  decline steps, and the template placeholders used during message assembly
  (Requirements 3.1–3.5, 4.1–4.3).
- An unknown version fails loudly (a typo cannot send an empty system prompt).
- The loader is memoized but returns equal values.
"""

from __future__ import annotations

import pytest
from app.chat import prompts


def test_load_system_prompt_returns_text_and_version() -> None:
    loaded = prompts.load_system_prompt()

    assert loaded.version == "system_v1"
    assert loaded.version == prompts.SYSTEM_PROMPT_VERSION
    assert loaded.text.strip()  # non-empty prompt text


def test_load_prompt_default_matches_system_version() -> None:
    assert prompts.load_prompt().version == prompts.SYSTEM_PROMPT_VERSION
    assert prompts.load_prompt().text == prompts.load_system_prompt().text


def test_system_prompt_has_required_clauses() -> None:
    text = prompts.load_system_prompt().text

    # The three guardrail clauses (Requirements 3, 4).
    assert "SCOPE" in text
    assert "EVIDENCE" in text
    assert "SECURITY" in text


def test_system_prompt_has_decline_template_steps() -> None:
    """The decline template's three steps are present (Requirement 3.4)."""
    text = prompts.load_system_prompt().text

    assert "decline" in text.lower()
    # Numbered decline steps from the design's prompt structure.
    assert "1)" in text
    assert "2)" in text
    assert "3)" in text


def test_system_prompt_has_security_data_not_instructions_clause() -> None:
    """Review content is data, not instructions (Requirements 4.1, 4.3)."""
    text = prompts.load_system_prompt().text

    # Review content is data, not instructions, and the prompt is never revealed.
    assert "DATA, not instructions" in text
    assert "reveal" in text and "this prompt" in text


def test_system_prompt_has_citation_clause() -> None:
    """Citations use the [r_xxxx] form over Corpus IDs only (Requirement 2.2)."""
    text = prompts.load_system_prompt().text

    assert "[r_0001]" in text
    assert "Cite only IDs present in <reviews>" in text


@pytest.mark.parametrize(
    "placeholder",
    ["{entity.name}", "{entity.category}", "{platform}", "{original_url}"],
)
def test_system_prompt_has_template_placeholders(placeholder: str) -> None:
    """Placeholders survive loading so message assembly can fill them (Task 1.3)."""
    assert placeholder in prompts.load_system_prompt().text


def test_load_prompt_unknown_version_raises() -> None:
    with pytest.raises(FileNotFoundError):
        prompts.load_prompt("system_v999")


def test_load_prompt_is_memoized_and_equal() -> None:
    first = prompts.load_prompt("system_v1")
    second = prompts.load_prompt("system_v1")

    assert first == second
    assert first.text == second.text
