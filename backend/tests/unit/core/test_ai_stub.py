"""Unit tests for the local AI stub and its production refusal (task 17)."""

from __future__ import annotations

import pytest
from app.core.ai_stub import ScriptedStubClient
from app.core.config import Settings


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {"ai_stub": True, "env": "local"}
    base.update(over)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_ai_stub_refused_in_production() -> None:
    """AI_STUB=1 with ENV=production must refuse to start (mirror SSRF guard)."""
    with pytest.raises(ValueError, match="AI_STUB must not be set when ENV=production"):
        _settings(env="production")


def test_ai_stub_allowed_outside_production() -> None:
    """AI_STUB is permitted in local/test environments."""
    assert _settings(env="local").ai_stub is True
    assert _settings(env="test").ai_stub is True


# A cleaned-page excerpt in the real cleaner format (see cleaner._render_line):
# "<indent>e<ref> <tag ...> \"text…\"". Leaf <p> lines with substantial text
# are the review bodies the stub should point at.
_CLEANED = (
    'e4 <main> "nav chrome and everything"\n'
    '  e7 <div> "Helen R. 5/5 ..."\n'
    '    e8 <p> "Grinds evenly and quietly, and the burrs feel genuinely sturdy."\n'
    '  e9 <div> "Tomas G. 4/5 ..."\n'
    '    e10 <p> "Great grind consistency for pour-over, but the hopper is small."\n'
    '  e16 <p> "short"\n'
)


def test_stub_locator_refs_come_from_the_page() -> None:
    """locator_result points at the real leaf review refs, not synthesized text."""
    client = ScriptedStubClient()
    resp = client.messages.create(
        messages=[{"role": "user", "content": _CLEANED}],
        tool_choice={"type": "tool", "name": "locator_result"},
        tools=[{"name": "locator_result"}],
    )
    data = resp.content[0].input
    assert data["has_reviews"] is True
    # e8 and e10 are the substantial <p> leaves; e16 "short" is excluded.
    assert [i["item_ref"] for i in data["items"]] == ["e8", "e10"]


def test_stub_dispatches_each_pipeline_tool() -> None:
    """profile / sentiment / themes each return their forced tool shape."""
    client = ScriptedStubClient()
    user = (
        'e1 <p> "A thoroughly good product that exceeded my expectations here."\n'
        'e2 <p> "Mediocre at best and the packaging arrived damaged in transit."\n'
        'e3 <p> "Absolutely fantastic value and I would buy it again tomorrow."'
    )

    prof = client.messages.create(
        messages=[{"role": "user", "content": user}], tool_choice={"name": "entity_profile"}
    )
    assert prof.content[0].name == "entity_profile"
    assert prof.content[0].input["name"]

    sent = client.messages.create(
        messages=[{"role": "user", "content": user}], tool_choice={"name": "classify_sentiment"}
    )
    assert len(sent.content[0].input["results"]) == 3

    themes = client.messages.create(
        messages=[{"role": "user", "content": user}], tool_choice={"name": "extract_themes"}
    )
    assert themes.content[0].input["themes"][0]["mentions"] == 3
