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
