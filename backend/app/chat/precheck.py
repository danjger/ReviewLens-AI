"""Scope pre-check for the guardrailed chat (Task 2).

A small, fast model call that classifies the user's question *before* the main
answer is generated. It is the design's **secondary** guardrail layer: it never
answers by itself, it only produces a hint. The system prompt (the primary
layer, Task 1.1) still decides, so the assistant's voice stays consistent and a
pre-check mistake can never put words in the assistant's mouth.

What it does (design "Scope pre-check"):

- One forced-tool call on the configured small/fast model (the ``precheck``
  purpose → ``CLAUDE_PRECHECK_MODEL``; model IDs come from config, never a
  literal). The forced tool's JSON schema is the only way the model may answer,
  so the result is always ``{label, category}`` — the model classifies, it does
  not write prose (Requirement 3.5).
- It returns ``label ∈ {in_scope, out_of_scope, injection, borderline}`` with an
  optional ``category`` (e.g. ``weather``, ``other_platform``).
- It runs **in parallel** with loading the Corpus (:func:`run_in_background`
  starts it on a thread; the endpoint loads the Corpus meanwhile) and has a
  **1.5-second timeout** (:data:`PRECHECK_TIMEOUT_S`). On timeout — or any error,
  or malformed output — the main call proceeds **without a hint**:
  :func:`await_result` returns ``None``, the safe default, so a slow or broken
  pre-check never blocks or weakens the answer.

How its result is used:

- Only ``out_of_scope`` and ``injection`` become a hint to the main model
  (:meth:`PrecheckResult.to_hint` → :class:`app.chat.assembly.PrecheckHint`).
  ``in_scope`` and ``borderline`` add nothing; the system prompt handles them.
- The result (label, category, latency) is recorded on the saved Exchange's
  ``scope``/``precheck`` fields together with the answer's own decline detection
  (post-processing, Task 3) — the pre-check is one input to the final scope tag,
  not the decision.

Every AI call goes through the instrumented client (:func:`app.core.ai.get_ai_client`),
so the global rate limit, logging, and the test stub all apply here too.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.chat.assembly import PrecheckHint
from app.chat.prompts import PRECHECK_PROMPT_VERSION, load_precheck_prompt
from app.core.ai import get_ai_client

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.chat.corpus import Corpus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Wall-clock budget for the pre-check (design: "a 1.5-second timeout"). The
#: pre-check runs in parallel with Corpus loading, so this bounds only the extra
#: wait the main call might incur; past it the main call proceeds with no hint.
PRECHECK_TIMEOUT_S = 1.5

#: AI purpose label → resolves to ``CLAUDE_PRECHECK_MODEL`` (a small, fast model).
_PURPOSE = "precheck"  # noqa: S105 - AI purpose label, not a secret

#: Max tokens for the pre-check's tool call. The output is a tiny
#: ``{label, category}`` object, so a small cap is plenty and keeps the call fast.
_MAX_TOKENS = 128

#: Name of the forced tool. The model must answer by calling exactly this tool.
_TOOL_NAME = "classify_scope"

#: The labels the classifier may return. ``out_of_scope`` and ``injection`` are
#: the only ones that become a hint (see :meth:`PrecheckResult.to_hint`).
_LABELS = ["in_scope", "out_of_scope", "injection", "borderline"]

#: The labels that are worth passing to the main model as a hint.
_HINTED_LABELS = frozenset({"out_of_scope", "injection"})


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrecheckResult:
    """A completed scope pre-check classification.

    :ivar label: One of :data:`_LABELS`.
    :ivar category: An optional sub-category (e.g. ``"weather"``,
        ``"other_platform"``, ``"injection"``); ``None`` for in-scope/borderline.
    :ivar latency_ms: How long the model call took, for the Exchange's
        ``precheck.latency_ms`` field and logging.
    :ivar prompt_version: The pre-check prompt version used (``precheck_v1``).
    """

    label: str
    category: str | None = None
    latency_ms: float = 0.0
    prompt_version: str = PRECHECK_PROMPT_VERSION

    def to_hint(self) -> PrecheckHint | None:
        """Return a :class:`PrecheckHint` only when the label is worth hinting.

        ``out_of_scope`` and ``injection`` become a hint for the main model;
        ``in_scope`` and ``borderline`` return ``None`` so nothing is added to
        the prompt and the system prompt alone decides (design: the pre-check
        never answers, it only nudges).
        """
        if self.label in _HINTED_LABELS:
            return PrecheckHint(label=self.label, category=self.category)
        return None


# ---------------------------------------------------------------------------
# Forced tool schema (JSON schema the model must answer through)
# ---------------------------------------------------------------------------


def tool_schema() -> dict[str, Any]:
    """Return the forced-tool definition for the pre-check call.

    The ``input_schema`` is the design's ``{label, category}`` contract: ``label``
    is required and constrained to :data:`_LABELS` by an enum, and ``category`` is
    a short optional string. Forcing the tool means the model can only classify —
    it has no field to write an answer in, which keeps the pre-check from ever
    answering on its own.
    """
    return {
        "name": _TOOL_NAME,
        "description": (
            "Classify the user's question's scope relative to one dataset of "
            "customer reviews. Return a label and a short category. Do not answer "
            "the question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "enum": _LABELS,
                    "description": "The scope classification.",
                },
                "category": {
                    "type": ["string", "null"],
                    "description": (
                        "Short sub-category: other_platform, world_knowledge, "
                        "competitor_facts, unrelated_task, or injection. Empty "
                        "for in_scope/borderline."
                    ),
                },
            },
            "required": ["label"],
            "additionalProperties": False,
        },
    }


# ---------------------------------------------------------------------------
# System prompt placeholder filling
# ---------------------------------------------------------------------------


def _fill_prompt(template: str, *, entity_name: str, entity_category: str, platform: str) -> str:
    """Fill the pre-check prompt's CONTEXT placeholders.

    Mirrors :func:`app.chat.assembly._fill_system_prompt`: the template uses
    ``{entity.name}``, ``{entity.category}``, and ``{platform}``, which are not
    valid ``str.format`` field names, so each known token is substituted
    explicitly and all other braces are left intact. Missing values get readable
    fallbacks so the prompt never shows a raw placeholder.
    """
    replacements = {
        "{entity.name}": entity_name or "this entity",
        "{entity.category}": entity_category or "unknown category",
        "{platform}": platform or "an uploaded file",
    }
    filled = template
    for token, value in replacements.items():
        filled = filled.replace(token, value)
    return filled


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _extract_tool_input(response: Any) -> dict[str, Any] | None:  # noqa: ANN401 - SDK message
    """Pull the forced tool's ``input`` dict out of an Anthropic message.

    Returns ``None`` (rather than raising) when the expected ``tool_use`` block
    is absent, so a malformed response degrades to "no hint" like a timeout
    rather than erroring out the whole chat request.
    """
    content = getattr(response, "content", None) or []
    for block in content:
        is_tool_use = getattr(block, "type", None) == "tool_use"
        is_our_tool = getattr(block, "name", None) == _TOOL_NAME
        if is_tool_use and is_our_tool:
            tool_input = getattr(block, "input", None)
            if isinstance(tool_input, dict):
                return tool_input
    return None


def parse_result(response: Any, *, latency_ms: float = 0.0) -> PrecheckResult | None:  # noqa: ANN401
    """Turn a model response into a :class:`PrecheckResult`, or ``None`` if invalid.

    Validation is deliberately lenient and never raises: the pre-check is a
    best-effort hint, so any malformed output (no tool block, missing label, or
    an unknown label) yields ``None`` — the safe default of "no hint" — instead
    of failing the request. A ``category`` that is empty, whitespace, or not a
    string is normalised to ``None``.

    :param response: The SDK message returned by the instrumented client.
    :param latency_ms: Measured call latency to record on the result.
    :returns: A :class:`PrecheckResult` when the label is one of :data:`_LABELS`;
        otherwise ``None``.
    """
    tool_input = _extract_tool_input(response)
    if tool_input is None:
        return None

    label = tool_input.get("label")
    if not isinstance(label, str) or label not in _LABELS:
        return None

    raw_category = tool_input.get("category")
    category: str | None = None
    if isinstance(raw_category, str) and raw_category.strip():
        category = raw_category.strip()

    return PrecheckResult(label=label, category=category, latency_ms=latency_ms)


# ---------------------------------------------------------------------------
# The model call
# ---------------------------------------------------------------------------


def classify(
    question: str, *, entity_name: str, entity_category: str, platform: str
) -> PrecheckResult | None:
    """Run the scope pre-check synchronously and return its result or ``None``.

    One forced-tool call on the small/fast model. Any failure — a provider error,
    the global AI limit, or malformed output — is swallowed and returns ``None``
    (no hint), because the pre-check must never break the main answer. Use
    :func:`run_in_background` + :func:`await_result` to apply the parallel
    execution and 1.5-second timeout the design requires.

    :param question: The analyst's question (sent as untrusted data).
    :param entity_name: The dataset entity's name, for the CONTEXT block.
    :param entity_category: The entity's category.
    :param platform: The dataset's source platform (``None``/empty for uploads).
    :returns: A :class:`PrecheckResult`, or ``None`` when the pre-check could not
        produce a valid classification.
    """
    system = _fill_prompt(
        load_precheck_prompt().text,
        entity_name=entity_name,
        entity_category=entity_category,
        platform=platform,
    )
    # The question is untrusted data: it is wrapped and never merged into the
    # system prompt, so an injection attempt in it cannot rewrite the classifier.
    user = f"<question>{question}</question>"

    start = time.monotonic()
    try:
        response = get_ai_client().create_message(
            purpose=_PURPOSE,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[tool_schema()],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            max_tokens=_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 - the pre-check must never break chat
        logger.info("precheck_failed error=%s", exc, extra={"event": "precheck_failed"})
        return None

    latency_ms = round((time.monotonic() - start) * 1000, 2)
    return parse_result(response, latency_ms=latency_ms)


def classify_corpus(
    corpus: Corpus, question: str, *, platform: str | None = None
) -> PrecheckResult | None:
    """Convenience wrapper: run :func:`classify` from a :class:`Corpus`.

    Pulls the entity name and category from the Corpus's :class:`EntityProfile`
    so the endpoint can call the pre-check with the same Corpus it loads for
    message assembly.
    """
    return classify(
        question,
        entity_name=corpus.entity.name,
        entity_category=corpus.entity.category or "",
        platform=platform or "",
    )


# ---------------------------------------------------------------------------
# Parallel execution + timeout
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PrecheckTask:
    """A running pre-check, started on a background thread.

    Returned by :func:`run_in_background` so the endpoint can start the pre-check,
    load the Corpus meanwhile, then collect the result with the timeout. Holds its
    own single-worker executor, shut down by :func:`await_result`.
    """

    _future: concurrent.futures.Future[PrecheckResult | None]
    _executor: concurrent.futures.ThreadPoolExecutor


def run_in_background(
    question: str, *, entity_name: str, entity_category: str, platform: str
) -> PrecheckTask:
    """Start the pre-check on a background thread and return immediately.

    The AI client is synchronous and releases the GIL on I/O, so running
    :func:`classify` on a thread lets the caller load the Corpus concurrently
    (design: the pre-check "runs in parallel with loading the Corpus"). Pair with
    :func:`await_result`, which enforces the 1.5-second timeout and cleans up.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        classify,
        question,
        entity_name=entity_name,
        entity_category=entity_category,
        platform=platform,
    )
    return PrecheckTask(_future=future, _executor=executor)


def await_result(
    task: PrecheckTask, *, timeout_s: float = PRECHECK_TIMEOUT_S
) -> PrecheckResult | None:
    """Collect a background pre-check's result within *timeout_s*, else ``None``.

    Waits up to *timeout_s* (default :data:`PRECHECK_TIMEOUT_S`) for the pre-check
    started by :func:`run_in_background`. On timeout the main call proceeds with
    **no hint**: this returns ``None`` and the still-running call is abandoned
    (the executor is shut down without waiting, so a slow pre-check can't delay
    the response). Any error raised inside the pre-check also yields ``None``
    because :func:`classify` already swallows its own failures.

    :param task: The :class:`PrecheckTask` from :func:`run_in_background`.
    :param timeout_s: Wall-clock budget to wait for the result.
    :returns: The :class:`PrecheckResult`, or ``None`` on timeout/error.
    """
    try:
        return task._future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        logger.info(
            "precheck_timeout timeout_s=%s",
            timeout_s,
            extra={"event": "precheck_timeout", "timeout_s": timeout_s},
        )
        task._future.cancel()
        return None
    except Exception as exc:  # noqa: BLE001 - defensive; classify() shouldn't raise
        logger.info("precheck_failed error=%s", exc, extra={"event": "precheck_failed"})
        return None
    finally:
        # Don't block the response on a pre-check that is still running past the
        # timeout; the worker thread finishes on its own and is discarded.
        task._executor.shutdown(wait=False)
