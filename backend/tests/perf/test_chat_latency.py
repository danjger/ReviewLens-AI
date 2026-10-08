"""Chat time-to-first-token performance test (guardrailed-chat task 8).

Requirement 7.2: *WHILE the Corpus has up to 1,000 reviews, the first token
SHALL appear within 5 seconds at the 90th percentile, excluding cold starts and
the first question after the Corpus prompt cache has expired.*

Requirement 7.3: *THE system SHALL cache the Corpus portion of the prompt so
that repeat questions on the same dataset cost less and respond faster.* The
assembled ``<reviews>`` block carries ``cache_control: {"type": "ephemeral"}``
(see :func:`app.chat.assembly._corpus_turn`); a cache hit on a repeat question
is reported by the model as ``cache_read_tokens`` on the streamed usage
(:meth:`app.core.ai.StreamUsage.as_dict`).

What this measures
------------------
This test drives the **real streaming path** against the **live model** so the
number it reports is a true time-to-first-token (TTFT): it synthesizes a
1,000-review Corpus, assembles the chat request exactly as the service does
(:func:`app.chat.assembly.assemble_messages` → the cached corpus turn), and
calls the single instrumented client's :meth:`app.core.ai.AiClient.stream_message`
(``purpose="chat"``; the model id comes from config, never a literal). TTFT is
the wall-clock from opening the stream to the first text chunk — the same chunk
the chat endpoint forwards as the first SSE ``token`` event (Requirement 7.1).

It asks a warmup question (the cold cache-*write* turn, which Requirement 7.2
excludes) followed by 20 measured questions back-to-back against the **same**
Corpus prefix, so every measured question hits the ephemeral Corpus cache. It
then asserts:

- **p90 TTFT < 5000 ms** over the 20 measured questions (Requirement 7.2), and
- **cache-read tokens > 0** on the repeat questions (Requirement 7.3), proving
  the Corpus prompt cache is actually being read rather than re-sent.

Gating (why a plain ``make test`` run skips cleanly)
---------------------------------------------------
This is a live-AI perf test, so it is gated twice and marked ``@pytest.mark.perf``
(like :mod:`tests.perf.test_api_latency`), keeping it out of ``make test`` /
``make test-int``:

- it **skips unless** ``PERF_LIVE_AI=1`` is set (the explicit live-AI perf
  opt-in), and
- it **skips unless** ``ANTHROPIC_API_KEY`` is present (it needs the real model).

So a normal run without ``PERF_LIVE_AI`` never spends money and never fails;
the bound is enforced only when the perf gate is opened.

The percentile helper mirrors :mod:`tests.perf.test_api_latency` (linear
interpolation); a small local copy is kept rather than importing across perf
test files to keep each perf test self-contained and runnable on its own.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
from app.chat import assembly
from app.chat.corpus import _parse_corpus
from app.core.ai import get_ai_client, reset_ai_client, resolve_model

pytestmark = pytest.mark.perf

# ── Tunables (overridable via env for CI / local runs) ───────────────────────

#: Number of reviews in the synthesized Corpus (the Requirement 7.2 ceiling).
SEED_REVIEW_COUNT = int(os.environ.get("PERF_CHAT_REVIEWS", "1000"))

#: Measured questions asked after the warmup. Requirement 7.2 is a p90 bound,
#: so a round 20 gives a stable percentile without a long/expensive run.
MEASURE_QUESTIONS = int(os.environ.get("PERF_CHAT_QUESTIONS", "20"))

#: The p90 TTFT bound from Requirement 7.2, in milliseconds.
P90_BUDGET_MS = float(os.environ.get("PERF_CHAT_P90_BUDGET_MS", "5000"))

#: Max tokens generated per answer. We only time the *first* chunk, so this is
#: kept small to bound spend — a short answer still yields a first token.
_CHAT_MAX_TOKENS = int(os.environ.get("PERF_CHAT_MAX_TOKENS", "256"))

#: A fixed, in-scope question set. All are answerable from the synthesized
#: Corpus; the exact wording does not matter for TTFT, only that each is a
#: distinct question sent against the same cached Corpus prefix. The list is
#: cycled to reach :data:`MEASURE_QUESTIONS`.
_QUESTION_BANK: tuple[str, ...] = (
    "What are the most common complaints in these reviews?",
    "What do reviewers like most about the product?",
    "How do reviewers describe the customer support?",
    "What do people say about the mobile experience?",
    "Are there recurring themes about pricing or value?",
    "What rating do most reviewers give, roughly?",
    "Do reviewers mention onboarding or setup?",
    "What negative feedback comes up more than once?",
    "What positive feedback comes up more than once?",
    "Do reviewers compare different features of the product?",
)

#: Scope fields for the synthesized dataset (only used to fill the prompt's
#: SCOPE line; no network dataset is read).
_PLATFORM = "fixtures"
_ORIGINAL_URL = "https://fixtures.example/perf-chat"


# ── p90 helper (linear interpolation; mirrors test_api_latency.percentile) ───


def percentile(samples: list[float], pct: float) -> float:
    """Return the ``pct`` percentile (0–100) of ``samples`` by linear interpolation.

    A local copy of :func:`tests.perf.test_api_latency.percentile` so this perf
    test is self-contained; both use the same method, cross-checked there
    against ``statistics.quantiles``. ``samples`` must be non-empty.
    """
    if not samples:
        raise ValueError("percentile() requires at least one sample")
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


# ── Corpus synthesis ─────────────────────────────────────────────────────────


def _make_reviews_doc(n: int) -> dict[str, Any]:
    """Build a ``reviews/v{n}.json``-shaped document with ``n`` reviews.

    The shape mirrors what review-analysis writes (entity profile + a ``reviews``
    list of ``{id, rating, date, text, ...}``) so it is parsed by the real
    :func:`app.chat.corpus._parse_corpus`, exercising the same assembly path a
    live dataset would. Review text is synthetic fixture data (this test seeds
    its own data; it never scrapes a site) and is sized to land in the design's
    ~100–150 tokens-per-review range so 1,000 reviews is a realistic ~100k-token
    Corpus.
    """
    sentiments = ("negative", "neutral", "positive")
    reviews = [
        {
            "id": f"r_{i:04d}",
            "rating": (i % 5) + 1,
            "date": f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
            "text": (
                f"Review {i}: this is synthetic review text used only to size the "
                f"corpus for a time-to-first-token measurement. It mentions setup, "
                f"support, reporting, pricing, and the mobile app so in-scope "
                f"questions have something to ground against. Sentiment {sentiments[i % 3]}."
            ),
            "author": f"Reviewer {i}",
            "title": f"Synthetic review {i}",
            "sentiment": sentiments[i % 3],
            "source_page": (i // 25) + 1,
        }
        for i in range(n)
    ]
    doc = {
        "dataset_id": "perf-chat-fixture",
        "version": 1,
        "entity": {
            "name": "Perf Fixture CRM",
            "category": "software",
            "description": "A synthetic CRM used only for chat latency measurement.",
            "confidence": "high",
        },
        "reviews": reviews,
    }
    return doc


# ── Measurement ──────────────────────────────────────────────────────────────


def _time_to_first_token(
    system: str, messages: list[dict[str, Any]]
) -> tuple[float, dict[str, int]]:
    """Stream one chat answer; return (ttft_ms, final usage dict).

    Opens the streaming call through the instrumented client and times the
    wall-clock from just before the stream is driven to the first non-empty text
    chunk (the first SSE ``token`` the endpoint would emit). The generator is
    then drained to completion so the usage counters (including
    ``cache_read_tokens``) are final, and the usage dict is returned.
    """
    result = get_ai_client().stream_message(
        purpose="chat",
        system=system,
        messages=messages,
        max_tokens=_CHAT_MAX_TOKENS,
    )
    start = time.perf_counter()
    ttft_ms: float | None = None
    for chunk in result.text_chunks:
        if chunk and ttft_ms is None:
            ttft_ms = (time.perf_counter() - start) * 1000.0
            # Keep draining so usage is final, but stop forwarding — the first
            # token is all Requirement 7.2 measures.
    if ttft_ms is None:
        # No text streamed at all (empty answer); treat as a miss so it surfaces
        # rather than silently dropping a sample.
        ttft_ms = (time.perf_counter() - start) * 1000.0
    return ttft_ms, result.usage.as_dict()


def _report(lines: list[str]) -> None:
    """Print the report and, in CI, append it to the GitHub step summary."""
    header = "## Chat TTFT (Req 7.2 / 7.3, 1,000 reviews)"
    print("\n" + header)  # noqa: T201 - perf report to console
    for line in lines:
        print("  " + line)  # noqa: T201

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(header + "\n\n")
            for line in lines:
                fh.write(f"- {line}\n")
            fh.write("\n")


# ── Skip gates ────────────────────────────────────────────────────────────────


def _perf_live_enabled() -> bool:
    """True when the live-AI perf gate ``PERF_LIVE_AI=1`` is set."""
    return os.environ.get("PERF_LIVE_AI") == "1"


# ── The test ──────────────────────────────────────────────────────────────────


def test_chat_ttft_p90_and_corpus_cache() -> None:
    """Req 7.2/7.3: p90 TTFT < 5 s and the Corpus prompt cache is read on repeats.

    Skips cleanly unless the live-AI perf gate is open (``PERF_LIVE_AI=1``) and
    an ``ANTHROPIC_API_KEY`` is configured, so a normal ``make test`` run never
    spends money and never fails on this test.

    When enabled it asks a warmup question (the excluded cold cache-write turn)
    then 20 measured questions back-to-back against the same 1,000-review Corpus
    prefix, asserting the p90 time-to-first-token stays under the 5-second budget
    and that repeat questions report cache-read tokens (the Corpus cache hit).
    """
    if not _perf_live_enabled():
        pytest.skip("PERF_LIVE_AI!=1; the chat latency perf test is a gated live-AI run")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set; the chat latency perf test needs the live model")

    # Use the REAL instrumented client, not the autouse offline stub. The backend
    # conftest points the process-wide client at ``FakeClaude`` for every test as
    # a network safety net; a gated live perf run deliberately opts out so it
    # streams against the configured model (``CLAUDE_CHAT_MODEL``). Resetting the
    # client makes ``get_ai_client()`` rebuild a real ``AiClient`` from the
    # environment; the conftest fixture restores the stub on teardown.
    reset_ai_client()

    # Arrange: a 1,000-review Corpus, assembled with the cached <reviews> block.
    corpus = _parse_corpus("perf-chat-fixture", 1, _make_reviews_doc(SEED_REVIEW_COUNT))
    assert len(corpus.reviews) == SEED_REVIEW_COUNT, (
        f"expected {SEED_REVIEW_COUNT} reviews in the Corpus, got {len(corpus.reviews)} "
        "(the token budget may have truncated it; raise CHAT_CORPUS_TOKEN_BUDGET for this run)"
    )
    conversation_id = "perf-chat"

    def assemble(question: str) -> tuple[str, list[dict[str, Any]]]:
        assembled = assembly.assemble_messages(
            corpus=corpus,
            question=question,
            conversation_id=conversation_id,
            history=(),  # TTFT per question; no prior turns needed
            platform=_PLATFORM,
            original_url=_ORIGINAL_URL,
            precheck=None,
        )
        return assembled.system, assembled.messages

    # Warmup: the first question writes the Corpus into the ephemeral cache. Its
    # TTFT is excluded from the p90 (Requirement 7.2 excludes the first question
    # after the cache has expired, i.e. the cache-write turn).
    warm_system, warm_messages = assemble(_QUESTION_BANK[0])
    warmup_ttft_ms, warmup_usage = _time_to_first_token(warm_system, warm_messages)

    # Measure: 20 questions back-to-back against the same cached Corpus prefix.
    ttfts: list[float] = []
    cache_reads: list[int] = []
    for i in range(MEASURE_QUESTIONS):
        question = _QUESTION_BANK[i % len(_QUESTION_BANK)]
        system, messages = assemble(question)
        ttft_ms, usage = _time_to_first_token(system, messages)
        ttfts.append(ttft_ms)
        cache_reads.append(usage.get("cache_read_tokens", 0))

    # Report (console + CI step summary) before asserting, so a failing run still
    # records the real numbers for the README analysis.
    p50 = percentile(ttfts, 50)
    p90 = percentile(ttfts, 90)
    mx = max(ttfts)
    repeat_cache_hits = sum(1 for c in cache_reads if c > 0)
    max_cache_read = max(cache_reads) if cache_reads else 0
    model_id = resolve_model("chat")  # from config, never a literal
    lines = [
        f"model={model_id} reviews={SEED_REVIEW_COUNT} corpus_tokens≈{corpus.estimated_tokens}",
        f"warmup(excluded) ttft={warmup_ttft_ms:.0f}ms "
        f"cache_write_tokens={warmup_usage.get('cache_read_tokens', 0)}",
        f"measured n={len(ttfts)} p50={p50:.0f}ms p90={p90:.0f}ms max={mx:.0f}ms "
        f"budget={P90_BUDGET_MS:.0f}ms",
        f"cache-read: {repeat_cache_hits}/{len(cache_reads)} measured questions hit the "
        f"Corpus cache (max cache_read_tokens={max_cache_read})",
    ]
    _report(lines)

    # Assert Requirement 7.3 first: the Corpus cache must actually be read on
    # repeats. If it is not, the TTFT numbers are not the cached-path numbers the
    # bound is about, so this is the more fundamental check.
    assert repeat_cache_hits > 0, (
        "No measured question reported cache_read_tokens > 0; the Corpus prompt "
        "cache was not read on repeats (Requirement 7.3). " + " | ".join(lines)
    )

    # Assert Requirement 7.2: p90 TTFT under the 5-second budget.
    assert p90 < P90_BUDGET_MS, (
        f"p90 time-to-first-token {p90:.0f}ms exceeded the {P90_BUDGET_MS:.0f}ms budget "
        f"(Requirement 7.2). " + " | ".join(lines)
    )


# ── Unit coverage for the pure percentile helper (runs in a normal suite) ────


def test_percentile_basic() -> None:
    """percentile() matches a known distribution and interpolates between points.

    This pure-helper check is not gated: it needs no live model, so it runs in a
    normal ``make test`` collection of the perf package and keeps the local copy
    honest against the shared method in ``test_api_latency``.
    """
    data = [float(x) for x in range(1, 101)]  # 1..100
    assert percentile(data, 90) == pytest.approx(90.1, abs=0.01)
    assert percentile(data, 50) == pytest.approx(50.5, abs=0.01)
    assert percentile([42.0], 90) == 42.0
    assert percentile([10.0, 20.0], 100) == 20.0
    assert percentile([10.0, 20.0], 0) == 10.0
