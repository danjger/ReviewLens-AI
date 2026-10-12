"""A deterministic, network-free AI stub for the LOCAL Compose stack.

Why this exists
---------------
The unit/integration suites inject a stub in-process via
:func:`app.core.ai.set_ai_client`, but the *running containers* always build a
real Anthropic client. That makes a local ``make e2e`` run depend on live AI
credits even though the E2E harness is meant to run with AI stubbed
(platform-foundation task 17: the stubbed run must NOT need an API key).

This module provides a scripted client that mimics the small
``messages.create(**kwargs)`` surface :class:`app.core.ai.AiClient` uses and
answers every forced-tool request deterministically, without any network call.
It is activated by configuration (``AI_STUB=1``) and is **refused in
production** (see ``core.config``), exactly like ``SSRF_TEST_ALLOW_HOSTS``.

Honesty / product rules
-----------------------
Review TEXT is never synthesized here. The ``locator_result`` response is
derived from the *rendered page lines* in the user message, so item refs
resolve against the real DOM — the model only points at elements, the code reads
the text from the page (steering: "AI output may point at elements; it may never
supply review text"). Only derived fields (entity profile, sentiment label,
themes) are synthesized, which is acceptable for a structural E2E run.

Promoted from the proven scripted stub in
``tests/integration/handlers/test_isolation_int.py`` so the container and the
tests share one behavior.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

#: Parses a cleaned-page line: ``<indent>e123 <tag ...> "text…" [attrs]``.
#: (See app.extraction.cleaner._render_line.) Captures ref, tag, and text.
_CLEANED_LINE_RE = re.compile(r'^\s*(e\d+)\s+<(\w+)[^>]*>(?:\s+"([^"]*)")?')

#: A review body line is a leaf text tag whose text is long enough to be a
#: review (not a label/nav). Deterministic and layout-agnostic — enough for the
#: local E2E seed fixtures; the real Locator does the nuanced job in production.
_REVIEW_TAGS = frozenset({"p"})
_MIN_REVIEW_TEXT = 40

#: Matches a Corpus review id in the assembled <reviews> JSON lines, e.g.
#: ``{"id": "r_0003", ...}`` — used by the streaming chat stub to cite a real
#: review so the answer produces a surviving citation chip.
_CORPUS_ID_RE = re.compile(r'"id"\s*:\s*"(r_\d+)"')


def _tool_use_response(tool_name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    """An SDK-shaped message carrying a single forced ``tool_use`` block."""
    block = SimpleNamespace(type="tool_use", name=tool_name, input=tool_input)
    return SimpleNamespace(
        id="msg_stub",
        content=[block],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def _text_response(text: str) -> SimpleNamespace:
    """An SDK-shaped plain-text message (for any non-tool call)."""
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(
        id="msg_stub",
        content=[block],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def _review_refs(user_text: str) -> list[str]:
    """Refs of likely review-body lines in the cleaned page (leaf text tags)."""
    refs: list[str] = []
    for line in user_text.splitlines():
        m = _CLEANED_LINE_RE.match(line)
        if not m:
            continue
        ref, tag, snippet = m.group(1), m.group(2), (m.group(3) or "")
        if tag in _REVIEW_TAGS and len(snippet) >= _MIN_REVIEW_TEXT:
            refs.append(ref)
    return refs


def _locator_from_user(user_text: str) -> dict[str, Any]:
    """Build a Locator tool input from the cleaned-page user content.

    Review refs are the leaf text lines in the cleaned page, so item/text refs
    resolve against the real DOM and the extractor reads the review text from
    the page itself (the model only points at elements, never supplies text).
    """
    refs = _review_refs(user_text)
    items = [{"item_ref": r, "text_ref": r, "kind": "review"} for r in refs]
    return {
        "has_reviews": bool(items),
        "rating_scale": 5,
        "items": items,
        "selectors": {"item": None, "text": None},
        "next_page": {"ref": None},
        "reported_total": len(items),
        "entity_hint": "Reviewed item",
        "confidence": "high",
    }


class _StubMessages:
    """``messages`` resource whose ``create`` dispatches by forced-tool name."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _tool_name(kwargs: dict[str, Any]) -> str:
        choice = kwargs.get("tool_choice") or {}
        if choice.get("name"):
            return str(choice["name"])
        tools = kwargs.get("tools") or []
        if tools:
            return str(tools[0].get("name", ""))
        return ""

    @staticmethod
    def _user_text(kwargs: dict[str, Any]) -> str:
        for message in kwargs.get("messages", []):
            if message.get("role") == "user":
                content = message.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
        return ""

    def _n_review_lines(self, user_text: str) -> int:
        return len(_review_refs(user_text))

    def create(self, **kwargs: Any) -> Any:  # noqa: ANN401 - mirrors SDK signature
        self.calls.append(kwargs)
        name = self._tool_name(kwargs)
        user_text = self._user_text(kwargs)

        # Streaming chat (chat service calls stream_message -> create(stream=True)).
        # Return an iterable of SDK-shaped stream events forming a short, in-scope
        # answer that cites a real Corpus review id, so the browser streams tokens
        # and the citation survives post-processing as a chip. (Local stub only;
        # the nuanced grounded answer is a live-model / needs-record-ai concern.)
        if kwargs.get("stream"):
            return self._stream_chat_answer(user_text)

        if name == "locator_result":
            return _tool_use_response("locator_result", _locator_from_user(user_text))
        if name == "entity_profile":
            return _tool_use_response(
                "entity_profile",
                {
                    "name": "Reviewed item",
                    "category": "Product",
                    "description": "An item that reviewers discuss.",
                    "confidence": "high",
                },
            )
        if name == "classify_sentiment":
            n = self._n_review_lines(user_text)
            return _tool_use_response(
                "classify_sentiment",
                {"results": [{"id": i, "sentiment": "positive"} for i in range(n)]},
            )
        if name == "extract_themes":
            n = self._n_review_lines(user_text)
            return _tool_use_response(
                "extract_themes",
                {
                    "themes": [
                        {
                            "label": "Overall experience",
                            "mentions": n,
                            "lean": "positive",
                            "example_ids": [str(i) for i in range(min(n, 3))],
                        }
                    ]
                },
            )
        # Any other (non-tool) call: a harmless, deterministic text answer. The
        # chat flow is exercised by a separate needs-record-ai E2E, not this stub.
        return _text_response("Stubbed response.")

    def _stream_chat_answer(self, user_text: str) -> Any:  # noqa: ANN401 - SDK stream
        """Yield SDK-shaped stream events for a cited, in-scope stub answer.

        Text deltas arrive on ``content_block_delta`` events (``delta.text``);
        usage arrives on ``message_start`` and a trailing ``message_delta`` —
        the exact shapes app.core.ai._stream_event_text / _accumulate_stream_usage
        read. The answer cites the first review id present in the Corpus so the
        citation is valid (post-processing drops any id not in the Corpus).
        """
        ids = _CORPUS_ID_RE.findall(user_text)
        first = ids[0] if ids else None
        if first is not None:
            answer = (
                "Based on the reviews, people most often praise the overall "
                f"experience and ease of setup [{first}]. This is a local stub "
                "answer grounded in the dataset's reviews."
            )
        else:
            answer = (
                "I can only answer from this dataset's reviews, and I could not "
                "find any to cite for that question."
            )

        def _events() -> Any:  # noqa: ANN401 - generator of SDK-shaped events
            # message_start carries input/cache usage.
            yield SimpleNamespace(
                type="message_start",
                message=SimpleNamespace(
                    usage=SimpleNamespace(
                        input_tokens=max(1, len(user_text) // 4),
                        output_tokens=0,
                        cache_creation_input_tokens=0,
                        cache_read_input_tokens=0,
                    )
                ),
            )
            # Stream the answer word-by-word as text_delta events.
            words = answer.split(" ")
            for i, word in enumerate(words):
                chunk = word if i == 0 else " " + word
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text=chunk),
                )
            # message_delta carries the final output token count.
            yield SimpleNamespace(
                type="message_delta",
                usage=SimpleNamespace(output_tokens=max(1, len(answer) // 4)),
            )

        return _events()

    def count_tokens(self, **kwargs: Any) -> Any:  # noqa: ANN401
        # A cheap deterministic estimate; the stub never calls the network.
        text = self._user_text(kwargs)
        return SimpleNamespace(input_tokens=max(1, len(text) // 4))


class ScriptedStubClient:
    """Anthropic-like client whose ``messages`` dispatches by tool name."""

    def __init__(self) -> None:
        self.messages = _StubMessages()
