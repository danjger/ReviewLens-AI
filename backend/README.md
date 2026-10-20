# ReviewLens AI — Backend

Python 3.12 / FastAPI services for ReviewLens AI.

The same code runs in two compute modes:

- **HTTP apps** — `app.api` (API service) and `app.chat` (chat/streaming), plain
  FastAPI served by uvicorn (local) or the AWS Lambda Web Adapter (AWS).
- **Background work** — one `Handler` per queue under `app/handlers`, run by
  `app.consumer` (the SQS Lambda adapter in AWS, a container poller locally).

## Layout

- `app/core` — config, logging, db, rate limiting, origin guard, errors, health
- `app/db` — SQLAlchemy models, status transitions, Alembic migrations
- `app/storage` — S3 key scheme and helpers
- `app/handlers`, `app/jobs` — queue handlers and scheduled jobs
- `app/ingestion`, `app/extraction`, `app/datasets`, `app/worker`, `app/chat`
  — domain modules
- `tests/` — `unit/`, `property/`, `integration/`, `scale/`, `perf/`

## Common commands

Run from the repository root:

- `make lint` — ruff, mypy, eslint, tsc
- `make test` — backend and frontend unit and property tests
- `make test-int` — integration tests against the Docker Compose stack

## AI processing

All AI in ReviewLens AI runs **in the backend** and goes through a single
instrumented client, `app/core/ai.py` (`AiClient` / `get_ai_client()`). This is
the only code that talks to the Anthropic API — nothing else constructs an
Anthropic client or embeds a model id. Routing this through one seam is what
makes the cost and data-leakage mitigations below enforceable rather than
per-call conventions.

### The six AI processing steps

| # | Step | Module | Purpose label | Model (config field) | What the model does |
|---|------|--------|---------------|----------------------|---------------------|
| 1 | URL pre-check | `app/chat/precheck.py` | `precheck` | `CLAUDE_PRECHECK_MODEL` (Haiku) | Reads the rendered page and judges whether it is a usable review page; informs the Will work / Limited / Won't work verdict. |
| 2 | Review Locator (extraction) | `app/extraction/locator.py` | `extract_locator` | `CLAUDE_EXTRACT_MODEL` (Haiku) | Returns a plan of **element references** for reviews/ratings/pagination. The review *text* is then read from the DOM by code — the model never emits review text. |
| 3 | Entity profile | `app/worker/ai/profile.py` | `profile` | `CLAUDE_EXTRACT_MODEL` | Summarizes what is being reviewed (name, category) from the extracted reviews. |
| 4 | Sentiment | `app/worker/ai/sentiment.py` | `sentiment` | `CLAUDE_EXTRACT_MODEL` | Classifies each review's sentiment by **review id** (ids in, labels out). |
| 5 | Themes | `app/worker/ai/themes.py` | `themes` | `CLAUDE_EXTRACT_MODEL` | Extracts recurring themes with mention counts and example review ids. |
| 6 | Guardrailed chat Q&A | `app/chat/service.py` | `chat` | `CLAUDE_CHAT_MODEL` (Sonnet) | Streams an answer grounded only in the dataset's reviews, with `[r_xxxx]` citations. |

Steps 2–5 run in the **processing pipeline** on the workers image (Playwright +
Chromium); step 1 runs in the API service; step 6 runs in the chat service.
Every call is a forced-tool request with a strict schema except chat, which
streams free text that is then validated (citations) in post-processing. Model
ids are resolved from `Settings` by the purpose label in
`app.core.ai.resolve_model` — never hard-coded.

### How it is implemented

- **One instrumented client.** `AiClient.create_message` (and
  `stream_message` for chat) wrap the Anthropic SDK. Each purpose maps to a
  configured model; an unknown purpose raises rather than silently picking a
  model.
- **Prompts are versioned files.** System/tool prompts live in
  `app/chat/prompts/` and `prompts/` (e.g. `system_v1.md`, `locator_v1.md`,
  `precheck_v1.md`). Changing one requires re-running the matching evaluation
  suite (`evals/extraction/`, `evals/scope_guard/`); the prompt version is saved
  with each chat Exchange.
- **Full-context grounding for chat.** The whole review Corpus for a dataset is
  assembled into a single prompt block (`app/chat/assembly.py`,
  `app/chat/corpus.py`) so answers and counts are exact.
- **Deterministic test stub.** In tests and the local Compose stack the client
  is swapped for `app/core/ai_stub.py` (`AI_STUB=1`), so no network call and no
  key/spend are needed. `AI_STUB` is refused when `ENV=production`.

### Cost mitigations

- **Global AI-call rate limit, checked before every call.** `AiClient` calls
  `check_rate_limit` on a single shared `ai_calls` counter
  (`RL_GLOBAL_AI_CALLS_PER_HOUR`, default 2000/hour) **before** invoking the
  model; over the limit raises `AiRateLimitError` and the model is never called.
  Every purpose draws from this one counter, so it caps total spend regardless
  of which step triggers it.
- **Per-IP request limits upstream.** `RL_CHECKS_PER_IP_HOUR` (default 30) and
  `RL_QUESTIONS_PER_IP_HOUR` (default 120) throttle the entry points
  (URL checks, questions) that fan out into AI calls.
- **Cheap model where cheap is enough.** Pre-check, extraction, profile,
  sentiment, and themes use Haiku; only chat uses Sonnet.
- **Prompt caching for chat.** The Corpus block is marked
  `cache_control: {"type": "ephemeral"}` (`app/chat/assembly.py`), so repeat
  questions on the same dataset re-read the cached ~100k–150k-token prefix
  instead of re-sending it. The `cache_read_tokens` are logged per call.
- **Bounded inputs.** `MAX_PAGES` (10) and `MAX_REVIEWS` (1000) cap how much is
  ever collected; `CHAT_CORPUS_TOKEN_BUDGET` (150k) trims the chat Corpus to a
  ceiling (with a truncation note) so one dataset cannot blow up a prompt.
- **Per-call cost telemetry.** Every call logs an `ai_call` line with purpose,
  model, latency, and input/output/cache tokens — the basis for cost tracking.
- **Account-level backstops (ops).** An AWS budget alarm and a hard monthly
  spend limit set in the Anthropic console sit behind the in-app limiter (see
  the root README "Cost controls").

### Data-leakage mitigations

- **Review text is page text, never generated.** The model may only point at
  DOM elements (step 2 returns element references; steps 4–5 operate on review
  **ids**); the review text shown anywhere is copied from the source page by
  code. Chat citations are validated against the Corpus and any `[r_xxxx]` that
  is not a real review id is dropped before the answer is shown or saved.
- **Scope guard on chat.** The system prompt (`app/chat/prompts/system_v1.md`)
  constrains answers to the dataset's reviews; out-of-scope questions (other
  platforms, world knowledge, competitors) are declined. This is graded by
  `evals/scope_guard/` (correct-decline and injection-resistance thresholds).
- **No raw PII in logs.** Client IPs are hashed with SHA-256 the moment they are
  received (`app/core/rate_limit.py` `hash_ip`); only the hash is stored in
  DynamoDB or written to logs. The `ai_call` log line carries purpose, model,
  latency, and token counts — not prompt or review content.
- **SSRF on every user-supplied fetch.** Any outbound fetch of a user-supplied
  URL — the URL probe, every browser sub-request during capture
  (`app/capture/engine.py`), robots.txt, and pagination — passes
  `assert_public_host`, so the rendering step cannot be steered into internal
  networks. `SSRF_TEST_ALLOW_HOSTS` is a local/CI-only escape hatch and is
  refused when `ENV=production`.
- **Secrets never in code.** `ANTHROPIC_API_KEY` is read from the environment
  (populated from Secrets Manager in AWS), never a literal; `.env` is gitignored.
- **No third-party egress beyond Anthropic.** The only outbound AI call is to
  Anthropic through this client; prompts carry the dataset's own review text and
  the user's question, nothing else.
