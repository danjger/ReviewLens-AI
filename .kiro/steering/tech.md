---
inclusion: always
---

# Tech stack and rules

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2 + Alembic, Pydantic v2, `httpx`, Playwright (Chromium), `selectolax`, `extruct`, `anthropic` SDK. Dependency management with `uv`.
- **Frontend:** React + TypeScript + Vite, TanStack Query, Recharts, Vitest + Testing Library.
- **Infrastructure:** AWS CDK (TypeScript). CloudFront + WAF, API Gateway (HTTP and WebSocket), Lambda with the AWS Lambda Web Adapter, SQS (standard and FIFO), EventBridge, DynamoDB, S3, Aurora PostgreSQL Serverless v2 with the RDS Data API, Secrets Manager.
- **Local:** Docker Compose runs every service as a container plus PostgreSQL 16, LocalStack, and a fixtures web server.
- **Tests:** pytest, Hypothesis (property-based), moto, LocalStack; Vitest, fast-check; Playwright for E2E.

## Commands

| Command | What it does |
|---|---|
| `make up` / `make down` | Start or stop the full local stack in Docker Compose |
| `make lint` | ruff, mypy, eslint, tsc |
| `make test` | Backend and frontend unit and property tests |
| `make test-int` | Integration tests against the Compose stack |
| `make test-scale` | Two instances of every consumer against shared queues |
| `make e2e` | Playwright E2E (AI stubbed unless `E2E_LIVE_AI=1`) |
| `make eval` | Extraction and guardrail evaluation suites (live AI; needs `ANTHROPIC_API_KEY`) |
| `make synth` | `cdk synth` plus CDK assertion tests |

## Non-negotiable engineering rules

- **Stateless services.** No correctness may depend on process memory or local disk. Shared state lives in Aurora, S3, DynamoDB, or SQS. `/tmp` is scratch only.
- **Same code, both compute modes.** HTTP apps are plain FastAPI. Background work is a `Handler` with `handle(body, meta)` run by `app.consumer`. Never import Lambda-specific event shapes outside `app.consumer`.
- **Idempotent handlers.** Every queue handler must tolerate duplicate and concurrent delivery (conditional writes, unique indexes, FIFO groups, `SKIP LOCKED`).
- **No VPC, no NAT.** Reach the database only through `core.db` (Data API in AWS, psycopg locally).
- **Every status change** goes through `db.status.transition()` or `db.status.log_event()`.
- **Every S3 key** comes from `storage.keys`.
- **Every AI call** goes through the instrumented client (logging, global rate limit, stub in tests). Model IDs come from config, never literals.
- **SSRF:** any outbound fetch of a user-supplied URL (probe, browser sub-requests, pagination) must pass `assert_public_host`.
- **Privacy:** never store or log raw client IPs; hash them.
- **Review text** comes from page elements via code. AI output may point at elements; it may never supply review text.
- **Prompts** live in versioned files under `prompts/`. Changing one requires running the matching evaluation.
- **Secrets** come from Secrets Manager or env; never commit them.

## Testing expectations

- Every task includes its tests. Don't mark a task complete until `make lint` and `make test` pass, and `make test-int` passes for tasks that touch queues, storage, or the database.
- Tests never call the live AI unless marked `@pytest.mark.live_ai` (skipped by default).
- Property tests reference the design's property number in their docstring, for example `"""Property 3: Lookup integrity"""`.
