# ReviewLens AI

A web portal for an Online Reputation Management (ORM) consultancy. Analysts provide review-page URLs or CSV files; the app checks whether it can read the reviews, collects them, summarizes them, and lets anyone ask questions grounded only in those reviews.

**No sign-in required.** Everyone with the link sees the same datasets, check results, and Q&A history.

---

## Table of Contents

1. [Architecture overview](#architecture-overview)
2. [Local setup](#local-setup)
3. [Environment variables](#environment-variables)
4. [Running tests](#running-tests)
5. [Compute modes](#compute-modes)
6. [Deployment](#deployment)
7. [Cost controls](#cost-controls)
8. [Engineering decisions](#engineering-decisions)
9. [Session logs](#session-logs)

---

## Architecture overview

```
Visitor browser
  └─► CloudFront + AWS WAF (per-IP rate rule)
        ├─► S3 (React SPA)
        ├─► API Gateway HTTP API → API service (FastAPI)
        ├─► Chat Function URL   → Chat service (FastAPI, streaming)
        └─► API Gateway WebSocket API ← Push consumer ← EventBridge → SQS

API service writes to:
  Aurora PostgreSQL Serverless v2 (via RDS Data API – no VPC, no NAT)
  S3 (datasets/, checks/, uploads/)
  DynamoDB (rate-limits, check-sessions, ws-connections)
  SQS check-queue, FIFO processing-queue

Job workers (Playwright + Chromium + Claude):
  SQS check-queue → URL check handler
  SQS processing-queue (FIFO, per-dataset group) → extraction + analysis handler

Sweeper: EventBridge Scheduler every 5 min → requeue stale datasets
```

No Service runs inside a VPC. The RDS Data API and Anthropic SDK reach Aurora and Claude directly over HTTPS, so there is no NAT gateway cost when idle.

The **initial deployment is Lambda mode** (near-zero idle cost). The same container images can run on ECS Fargate without code changes; see [Compute modes](#compute-modes).

---

## Local setup

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) ≥ 4.x
- [uv](https://docs.astral.sh/uv/) (Python package manager) – `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [Node.js](https://nodejs.org/) ≥ 20 (for the frontend and CDK)
- [AWS CDK CLI](https://docs.aws.amazon.com/cdk/latest/guide/cli.html) – `npm install -g aws-cdk`

### Steps

```bash
# 1. Clone the repo
git clone https://github.com/your-org/reviewlens-ai.git
cd reviewlens-ai

# 2. Copy environment config
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY (and anything else marked CHANGEME)

# 3. Install backend dev dependencies
cd backend && uv sync && cd ..

# 4. Install frontend dependencies
cd frontend && npm install && cd ..

# 5. Install infra dependencies
cd infra && npm install && cd ..

# 6. Start the full local stack (PostgreSQL, LocalStack, all services)
make up

# Services are available at:
#   API:      http://localhost:8000
#   Frontend: http://localhost:5173  (run separately with: cd frontend && npm run dev)
#   LocalStack dashboard: http://localhost:4566

# 7. Stop everything
make down
```

---

## Environment variables

See [`.env.example`](.env.example) for the full list. Key variables:

| Variable | Description | Default |
|---|---|---|
| `ENV` | Runtime environment | `local` |
| `DATABASE_URL` | PostgreSQL connection string (local) | postgres://... |
| `ANTHROPIC_API_KEY` | Anthropic API key – **never commit** | – |
| `ORIGIN_VERIFY_SECRET` | Secret header shared with CloudFront | – |
| `S3_BUCKET` | S3 bucket name | `reviewlens-local` |
| `CHECK_QUEUE_URL` | SQS check queue URL | LocalStack default |
| `PROCESSING_QUEUE_URL` | SQS FIFO processing queue URL | LocalStack default |
| `PUSH_QUEUE_URL` | SQS push queue URL | LocalStack default |
| `CLAUDE_CHAT_MODEL` | Model ID for Q&A | `claude-sonnet-5-5` |
| `CLAUDE_EXTRACT_MODEL` | Model ID for extraction | `claude-haiku-5-5` |
| `CLAUDE_PRECHECK_MODEL` | Model ID for URL pre-check | `claude-haiku-5-5` |
| `RL_CHECKS_PER_IP_HOUR` | Per-IP check rate limit | `30` |
| `RL_QUESTIONS_PER_IP_HOUR` | Per-IP question rate limit | `120` |
| `RL_GLOBAL_AI_CALLS_PER_HOUR` | Global AI call rate limit | `2000` |
| `SSRF_TEST_ALLOW_HOSTS` | Extra hosts for SSRF bypass – **local/test only** | `` |
| `MAX_PAGES` | Max pages to crawl per dataset | `10` |
| `MAX_REVIEWS` | Max reviews per dataset | `1000` |

> **Security note:** `SSRF_TEST_ALLOW_HOSTS` is provided for local and CI testing only. The app refuses to start in production (`ENV=production`) when this variable is set.

---

## Running tests

```bash
# Lint (ruff, mypy, eslint, tsc)
make lint

# Unit + property tests (no live AI, no Docker required)
make test

# Integration tests against the full Compose stack
make test-int

# Scale test (two consumer instances, shared queues)
make test-scale

# Playwright E2E (AI stubbed)
make e2e

# E2E with live AI (needs ANTHROPIC_API_KEY)
E2E_LIVE_AI=1 make e2e

# Extraction + guardrail evals (live AI)
make eval

# CDK synth + CDK assertion tests
make synth
```

Tests never call the live AI unless marked `@pytest.mark.live_ai` (backend) or `E2E_LIVE_AI=1` (E2E).

### Extraction evaluation results

The extraction evaluation (`evals/extraction/`) scores the review-extraction
engine against **21 hand-labeled pages** — the required layout types (structured
JSON-LD/microdata, client-rendered, plain lists, no-ratings, multi-page,
mixed Q&A/seller-response, blockers) plus faithful reconstructions of real
review-platform markup and real saved pages from a public scraping sandbox (see
`evals/extraction/pages/SOURCES.md`). It scores each method and the automatic
choice, and asserts the Requirement 8.3 extraction thresholds and the
dataset-ingestion Requirement 3.14 verdict-accuracy threshold:

```bash
cd backend && uv run pytest ../evals -v -m live_ai   # needs ANTHROPIC_API_KEY
# or (numbers only, no assert):  python evals/extraction/run.py --live
```

Latest live run (extraction model `claude-haiku-5-5`):

> **⚠️ Haiku 5.5 does not currently meet the recall gate on this corpus.** The
> live run below shows recall **86.0%** (threshold ≥ 90%) — one page
> (`real_webscrapingdev_reviews`) under-extracts and flips `will_work`→`limited`.
> The model upgrade (PR #30) is **blocked** on recovering recall; the shipping
> default remains `claude-haiku-4-5-20251001` until this passes. Precision and
> the scope-guard suite are unaffected.

| Metric | Threshold | Result (Haiku 5.5) |
|---|---|---|
| `auto` precision on `will_work` pages | ≥ 98% | **99.2%** ✓ |
| `auto` recall on `will_work` pages | ≥ 90% | **86.0%** ✗ |
| Viability verdict accuracy | ≥ 90% | **95.2%** (20/21) ✓ |

For reference, Haiku 4.5 passed all three (precision 99.3%, recall 100.0%,
verdict 95–100%).

**Default strategy — `EXTRACTION_STRATEGY=auto`.** The method totals make the
choice clear: `auto` reaches 99.3% precision / 100% recall across the corpus,
whereas `structured` alone recalls ~10% (most pages carry no schema.org data)
and the pure `selectors` method needs a known-good selector set that only the
AI plan produces at runtime. `auto` (plan-then-extract) is therefore the default
in `core.config` and is what these thresholds are measured against.

Note: the viability verdict uses a live model call and is **non-deterministic on
borderline pages** — e.g. a page that shows 5 reviews but advertises thousands
with no next-page link is correctly `limited`, but one such case occasionally
flips `limited`↔`will_work` across runs. Accuracy stays at or above the 90%
gate (observed 20–21 of 21). The `auto` precision/recall was stable across runs.
The full per-method, per-page breakdown is written to
[`evals/extraction/report.md`](evals/extraction/report.md) on every run.

### Scope-guard evaluation results

The guardrail evaluation (`evals/scope_guard/`) grades 61 labeled questions — in-scope,
out-of-scope (other platforms, world knowledge/weather, competitor facts, unrelated tasks),
borderline, and prompt-injection attempts — through the live chat flow (system prompt →
model → post-processing) plus an LLM-as-judge, and asserts the Requirement 8.3 thresholds.
It runs on demand and whenever a chat prompt file changes:

```bash
cd backend && uv run pytest ../evals/scope_guard -v -m live_ai   # needs ANTHROPIC_API_KEY
# or:  python evals/scope_guard/run.py --live
```

Latest live run (`prompts/system_v1.md`, model `claude-sonnet-5`):

| Metric | Threshold | Result |
|---|---|---|
| Correct-decline rate (out-of-scope + injection) | ≥ 95% | **100.0%** (35/35) ✓ |
| False-decline rate (in-scope + borderline + competitor "what reviewers say") | ≤ 5% | **0.0%** (0/26) ✓ |
| Injection successes | 0 | **0** of 7 ✓ |
| Citation validity | 100% | **100.0%** (26/26 answers) ✓ |

All thresholds pass. The full per-case breakdown is written to
[`evals/scope_guard/report.md`](evals/scope_guard/report.md) on every run.

### Chat latency results

The chat performance test (`backend/tests/perf/test_chat_latency.py`) measures the
streaming time-to-first-token (TTFT) against a synthesized 1,000-review Corpus and
verifies the Corpus prompt cache (Requirements 7.2 and 7.3). It drives the real
streaming path — assemble the request (with the cache-marked `<reviews>` block),
then `AiClient.stream_message` — and times the wall-clock to the first streamed
token. It is marked `@pytest.mark.perf` and gated behind `PERF_LIVE_AI=1` (plus
`ANTHROPIC_API_KEY`), so a normal `make test` run skips it cleanly:

```bash
cd backend && PERF_LIVE_AI=1 uv run pytest tests/perf/test_chat_latency.py -s  # needs ANTHROPIC_API_KEY
```

It asks a warmup question (the cold cache-*write* turn, excluded per Requirement
7.2) then 20 measured questions back-to-back against the same cached Corpus prefix.

Latest live run (model `claude-sonnet-5`, 1,000 reviews, Corpus ≈ 67k tokens):

| Metric | Threshold | Result |
|---|---|---|
| Time-to-first-token, p90 (repeat questions) | < 5,000 ms | **2,743 ms** ✓ (p50 930 ms, max 3,321 ms) |
| Corpus prompt-cache hit on repeats | cache_read_tokens > 0 | **20/20** questions hit the cache (up to 115,150 cache-read tokens) ✓ |

The excluded warmup (cache-write) question had a TTFT of 1,633 ms. Both bounds
pass: repeat questions start streaming well under the 5-second budget and read the
cached ~67k-token Corpus rather than re-sending it.

---

## Compute modes

Every service is a stateless container image. The same image runs in two modes:

| Mode | How it runs | When to use |
|---|---|---|
| **Lambda** (default) | AWS Lambda + Lambda Web Adapter; SQS event-source mappings for consumers | Initial deployment; near-zero idle cost |
| **Container** | ECS Fargate; `uvicorn app.api:app` for HTTP services; `python -m app.consumer` for queue workers | When sustained load makes Lambda more expensive than containers |

Switch between modes by changing the `computeMode` context value in `infra/cdk.json` (`lambda` → `container`). No application code changes are needed.

Local development always runs in container mode via Docker Compose.

---

## Deployment

### First deploy (one time, needs a person)

1. Bootstrap the CDK environment:
   ```bash
   cdk bootstrap aws://ACCOUNT_ID/REGION
   ```
2. Deploy the GitHub OIDC stack (creates the deploy role used by CI):
   ```bash
   cd infra && npx cdk deploy GithubOidcStack
   ```
3. Store secrets in AWS Secrets Manager (database credentials, `ANTHROPIC_API_KEY`, `ORIGIN_VERIFY_SECRET`).
4. Set the monthly budget alarm email (see `CostStack` in CDK).

### Automated deploys (CI/CD)

Merging to `main` triggers the GitHub Actions pipeline:
1. Lint, type-check, unit tests, image build
2. Container integration tests (`docker-compose`)
3. Scale test
4. `cdk synth` + CDK assertion tests
5. Push image digests to ECR
6. Run Alembic migrations (one-off ECS task)
7. `cdk deploy --all` via GitHub OIDC
8. Upload the frontend build to S3, invalidate CloudFront
9. Smoke test against the Lambda deployment (Data API read + write)

The pipeline never deploys on a failed test step.

#### When CI runs (keeping Actions usage down)

CI triggers on **pull requests** and on **push to `main`** only — a push to a
feature branch with an open PR runs CI once (via the PR), not twice, and a
push to a branch with no PR runs nothing. Superseded commits are cancelled.

Jobs are split into two tiers:

- **Always run** (every PR, and push to `main`): lint + type check,
  unit/property tests, and the container image build. Fast signal, no Docker
  Compose stack.
- **Heavy / opt-in** (container integration, scale, CDK synth, non-blocking
  perf): run only when a push targets `main`, a PR's base is `main`, or the PR
  carries the **`full-ci`** label — and only for the areas that changed
  (a `changes` job path-filters backend / frontend / infra, so docs-only PRs
  skip the container jobs and CDK synth runs only when `infra/**` changes).

Everything that reaches `main` still runs the full pipeline before deploy, so
the deploy gate and production safety are unchanged — only *when* the heavy
jobs run on feature branches is reduced. To force the full pipeline on a PR
that doesn't target `main`, add the `full-ci` label.

---

## Cost controls

Two layers protect against runaway costs:

### AWS Budget alarm
An `AWS::Budgets::Budget` resource (in the `CostStack` CDK stack) sends an email notification when monthly spend is projected to exceed a configured threshold (default: **$50/month**). Set the email address in `infra/cdk.json` under `budgetAlertEmail`.

### Anthropic spend limit
Set a hard monthly spend limit in the [Anthropic console](https://console.anthropic.com/) under **Account → Billing → Usage limits**. The app also enforces a configurable global rate limit (`RL_GLOBAL_AI_CALLS_PER_HOUR`) in DynamoDB as a second line of defence.

Both controls are documented here and are required before going live (Requirement 7.5).

### Other cost drivers at scale
- **Aurora PostgreSQL Serverless v2** auto-pauses after 5 minutes of inactivity. The first request after a pause takes ~15 seconds while it resumes — acceptable for a prototype.
- **Lambda** costs essentially nothing when idle.
- **CloudFront** serves the SPA from the CDN, keeping origin requests low.
- **S3 lifecycle rules** delete `checks/` and `uploads/` objects after 1 day.


---

## Engineering decisions

Key decisions made while building and maintaining this project, with the
reasoning behind each. Product-level decisions (no sign-in, AI-first extraction,
Aurora via the Data API, Anthropic Claude) live in
[`.kiro/steering/product.md`](.kiro/steering/product.md); this section records
the engineering and process decisions that shaped the codebase.

### Architecture & platform
- **Stateless services, same image two ways.** Every backend service is a
  stateless container that runs unchanged on Lambda (first deployment, near-zero
  idle cost) or ECS Fargate. Shared state lives only in Aurora, S3, DynamoDB, or
  SQS. See [Compute modes](#compute-modes).
- **No VPC, no NAT.** Aurora is reached through the RDS Data API and Anthropic
  over HTTPS, so there is no NAT gateway cost when idle.
- **Aurora PostgreSQL Serverless v2 via the Data API**, auto-pausing when idle;
  a prototype accepts the ~15s cold-resume for near-zero idle cost.
- **FIFO processing queue, per-dataset message group**, so one dataset is never
  processed concurrently while different datasets still parallelize.
- **Queue messages carry IDs only**; payloads live in S3 or the database.

### Extraction & AI
- **AI-first, no site-specific parsers.** Review detection is dynamic; quality
  is measured by the evaluation suite rather than brittle per-site scrapers.
- **Default `EXTRACTION_STRATEGY=auto`** (plan-then-extract), chosen from the
  evaluation report: `auto` reached 99.3% precision / 100% recall, versus
  `structured` alone (~10% recall) or `selectors` (needs an AI-built plan). See
  [Extraction evaluation results](#extraction-evaluation-results).
- **Review text is always copied from the page, never AI-generated.** The AI may
  point at elements; it may never supply review text.
- **Model IDs come from config, never literals**, and every AI call goes through
  one instrumented client (logging, global rate limit, test stub).

### Security & cost
- **No sign-in; abuse controlled by layers** — WAF, per-IP and global rate
  limits, a budget alarm, and an Anthropic spend limit. See
  [Cost controls](#cost-controls).
- **SSRF guard on every user-supplied outbound fetch**; `SSRF_TEST_ALLOW_HOSTS`
  is refused when `ENV=production`.
- **Client IPs are hashed, never stored or logged raw.**
- **Secrets come from Secrets Manager or env, never committed.** `.env` is
  gitignored; CI uses the secret-free `.env.example`.

### Testing & tooling (notable fixes)
- **`make test-int` runs datastores only; `make test-scale` keeps the
  consumers.** Integration tests own the queues they assert on, so the live
  queue consumers must be absent during `test-int` or they race the tests; the
  scale suite is the opposite and asserts on those consumers.
- **The test harness loads the repo-root `.env`** (guarded by `if [ -f .env ]`),
  and CI copies `.env.example` to `.env` before `make test-int`. Before this,
  ~140 DB-backed integration tests silently self-skipped (locally and in CI)
  because pydantic resolves `.env` relative to the working directory and the
  tests run from `backend/`. Details in
  [`docs/known-issues-live-stack.md`](docs/known-issues-live-stack.md).
- **Property tests reference their design property number**, and tests never
  call the live model unless explicitly marked.

### Branding & frontend
- **Design tokens are the single styling source.** The whole UI reads CSS custom
  properties from `frontend/src/styles/theme.css`, so a re-theme is a one-file
  change. The brand ("Warm Amber": orange/yellow/brown, rounded, thin borders,
  generous spacing, sans-serif) is defined in
  [`.kiro/steering/branding.md`](.kiro/steering/branding.md) and applied through
  those tokens — components reference tokens, never hard-coded values.
- **Palette tuned for WCAG AA** on normal-size text in both light and dark mode.
  Full WCAG conformance still requires manual assistive-tech testing.

### Process
- **Feature branches + pull requests**, merged only after CI is green and with
  explicit confirmation — never a direct push to `main`.
- **Spec build order is fixed** (`platform-foundation` → `review-extraction` →
  `dataset-ingestion` → `review-analysis` → `dataset-library` →
  `ingestion-summary` → `guardrailed-chat`); a task reuses earlier specs'
  implementations rather than re-implementing them. See
  [`.kiro/steering/spec-workflow.md`](.kiro/steering/spec-workflow.md).

---

## Session logs

Readable summaries of the working sessions with Kiro — what was requested, what
changed, and the decisions made — live in [`sessions/`](sessions/). They are
historical records; when a note disagrees with the specs, steering, or code,
those win.
