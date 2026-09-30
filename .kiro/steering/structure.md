---
inclusion: always
---

# Repository structure and conventions

```
/infra                CDK app (TypeScript). One stack per concern: Data, Edge, Api, Workers, Realtime, Frontend, Cost, TestFixtures, optional Containers
/backend
  Dockerfile          backend image (API, chat, push consumer, sweeper)
  Dockerfile.workers  workers image (adds Playwright + Chromium)
  /app
    api.py            FastAPI app: API service
    chat.py           FastAPI app: chat service (streaming)
    consumer.py       queue consumer runtime (Lambda SQS adapter + container poller)
    /handlers         check.py, processing.py, push.py (one Handler per queue)
    /jobs             sweep.py
    /core             config, logging, db, rate_limit, origin_guard, errors, health
    /db               models, status.py (transition/log_event), migrations/
    /storage          keys.py and S3 helpers
    /events           EventBridge publisher
    /capture          Playwright rendering (workers image only)
    /extraction       Extraction Engine (spec: review-extraction)
    /ingestion        normalizer, validator, robots, viability, service, upload parser
    /datasets         refresh_service, library queries
    /chat             prompts/, assembly, precheck, postprocess
    /worker/ai        profile, sentiment, themes
  /prompts            versioned prompt files (locator_v1.md, system_v1.md, precheck_v1.md)
  /tests              unit/, property/, integration/, scale/, perf/
/frontend/src         routes/, components/, hooks/ (useRealtime), api/
/e2e                  Playwright tests
/fixtures-site        static review pages used by tests and the evaluation suites
/evals                extraction/ and scope_guard/
/docs                 brief.md and other project docs
.kiro/specs           one folder per feature spec
.kiro/steering        these files
```

## Conventions

- Python: `snake_case` modules and functions, `PascalCase` classes, type hints everywhere, `mypy --strict` on `app/`.
- TypeScript: `PascalCase` components, `camelCase` hooks prefixed with `use`, one component per file.
- API routes are prefixed `/api`; chat streaming is `/api/chat/...`.
- Errors return `{ "error": { "code", "message" } }`; codes are `UPPER_SNAKE_CASE`.
- Queue message bodies are small JSON objects with IDs only; payloads live in S3 or the database.
- A test file mirrors the module it tests: `app/ingestion/normalizer.py` → `tests/unit/ingestion/test_normalizer.py`.
- Migrations are one Alembic revision per spec task that changes the schema.
