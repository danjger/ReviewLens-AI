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
| `CLAUDE_CHAT_MODEL` | Model ID for Q&A | `claude-3-5-sonnet-*` |
| `CLAUDE_EXTRACT_MODEL` | Model ID for extraction | `claude-3-5-haiku-*` |
| `CLAUDE_PRECHECK_MODEL` | Model ID for URL pre-check | `claude-3-5-haiku-*` |
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
