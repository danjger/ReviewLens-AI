# Implementation Plan

- [x] 1. Scaffold the repository and tooling
  - Create the `/infra`, `/backend`, `/frontend`, `/e2e`, `/fixtures-site`, and `/evals` directories as laid out in the design
  - Add ruff, mypy, and pytest config for the backend; eslint, tsc, and Vitest for the frontend
  - Add `.env.example` that lists every required variable
  - Add a `Makefile` with the commands listed in `.kiro/steering/tech.md` (`make up`, `make down`, `make lint`, `make test`, `make test-int`, `make test-scale`, `make e2e`, `make eval`, `make synth`)
  - Write a README with local setup, environment variables, deploy steps, compute modes, cost controls, and an architecture summary
  - _Requirements: 1.2, 1.5, 6.2, 7.5_

- [x] 2. Build the container images and local environment
  - [x] 2.1 Write `Dockerfile` (backend) and `Dockerfile.workers` (backend plus Playwright and Chromium), both including the AWS Lambda Web Adapter so they run on Lambda and as plain containers
    - _Requirements: 3.2, 3.3_
  - [x] 2.2 Write `docker-compose.yml` running the API, chat, job workers, push consumer, and sweeper as containers, plus PostgreSQL, LocalStack, and a fixtures container serving `/fixtures-site`
    - _Requirements: 3.8, 8.6_
  - [x] 2.3 Add `/healthz` and `/readyz` to every HTTP Service and to the consumer runtime (small HTTP listener in container mode)
    - _Requirements: 3.7_

- [x] 3. Implement shared backend core
  - [x] 3.1 Implement `core.config.Settings` with all tunable limits and Secrets Manager loading
    - Fail startup when `SSRF_TEST_ALLOW_HOSTS` is set and `ENV=production`
    - Write unit tests for defaults, overrides, a missing secret, and the production allowlist refusal
    - _Requirements: 6.1, 6.3, 8.6_
  - [x] 3.2 Implement the structured JSON logger with service, instance, correlation, and dataset context
    - Add FastAPI middleware that assigns a correlation ID to each request
    - Write unit tests that assert the log shape
    - _Requirements: 9.1_
  - [x] 3.3 Implement the global error handler that returns the generic error envelope
    - Write unit tests for handled errors and unhandled exceptions
    - _Requirements: 9.2_
  - [x] 3.4 Implement `storage.keys` and the S3 helper functions, including the `checks/` and `uploads/` prefixes
    - Write unit tests for every key builder
    - _Requirements: 5.1_
  - [x] 3.5 Implement `core.origin_guard` middleware
    - Write unit tests for missing, wrong, and correct headers and the health-endpoint exemption
    - _Requirements: 2.3_
  - [x] 3.6 Implement `core.rate_limit` with DynamoDB fixed-window counters keyed by hashed client IP and globally, returning 429 with `Retry-After`
    - Write unit tests for window rollover, per-IP limits, and global limits
    - _Requirements: 2.4, 2.5_

- [x] 4. Implement the queue consumer runtime
  - [x] 4.1 Implement `app.consumer` with the `Handler` protocol, the Lambda SQS adapter (partial batch failures), and the container long-poll loop (visibility heartbeat, graceful `SIGTERM`)
    - _Requirements: 3.4, 3.7_
  - [x] 4.2 Write unit tests for both adapters calling the same handler, final-attempt detection from the receive count, heartbeat extension, and shutdown mid-message
    - _Requirements: 3.4, 3.5, 3.7_

- [x] 5. Implement the data layer
  - [x] 5.1 Implement `core.db` with the Data API driver in AWS and psycopg locally
    - _Requirements: 4.1, 7.4_
  - [x] 5.2 Create the SQLAlchemy `Dataset` and `DatasetVersion` models and the first Alembic migration
    - Include the status enum constraint, JSONB columns, `data_version` and `active_version`, the normalized URL columns, and indexes (the unique normalized-URL index can land here or in `dataset-ingestion` task 5)
    - _Requirements: 4.2, 4.3, 4.5, 4.6_
  - [x] 5.3 Implement `db.status.transition()` and `db.status.log_event()` with an atomic update, a `status_detail` append, and an EventBridge publish
    - Write unit tests with moto for EventBridge
    - Write integration tests against PostgreSQL that confirm the event history is kept in order
    - _Requirements: 4.4_

- [x] 6. Define infrastructure with CDK
  - [x] 6.1 Data stack: Aurora PostgreSQL Serverless v2 (min 0 ACU, Data API enabled), a private S3 bucket with SSE, public access blocked, and lifecycle rules deleting `checks/` and `uploads/` after 1 day, DynamoDB tables (`rate-limits`, `check-sessions`, `ws-connections`), and Secrets Manager entries
    - _Requirements: 4.1, 5.1, 5.2, 5.3, 6.1, 7.1, 7.4_
  - [x] 6.2 Edge stack: CloudFront distribution with behaviors for the SPA, `/api/*`, and `/api/chat/*`; AWS WAF with a per-IP rate-based rule and managed common rules; the `X-Origin-Verify` custom origin header
    - _Requirements: 1.1, 2.2, 2.3, 7.2_
  - [x] 6.3 Api stack in Lambda mode: API service Lambda (container image, no VPC) behind an HTTP API; chat service Lambda with a streaming Function URL; EventBridge bus; `check-queue`, FIFO `processing-queue`, and `push-queue` with DLQs
    - _Requirements: 1.1, 3.2, 7.1, 7.4_
  - [x] 6.4 Workers stack in Lambda mode: job-worker Lambdas (workers image) with SQS event sources, the push consumer Lambda, the DLQ consumer, and the sweeper Lambda on an EventBridge Scheduler schedule
    - _Requirements: 3.4, 3.6_
  - [x] 6.5 Organize the CDK code so each Service reads a `computeMode` context value, with `lambda` implemented and `container` wired to the optional Containers stack
    - _Requirements: 3.9_
  - [x] 6.6 Frontend stack: S3 origin for the SPA
    - _Requirements: 7.2_
  - [x] 6.7 Cost stack: AWS Budgets monthly alarm with email notification
    - _Requirements: 7.5_
  - [x] 6.8 Test fixtures stack: public S3 + CloudFront site publishing `/fixtures-site`, deployed only to non-production accounts
    - _Requirements: 8.6_
  - [x] 6.9 Write CDK assertion tests: bucket encryption, blocked public access, lifecycle rules, WAF attached with a rate rule, origin header configured, no Lambda attached to a VPC, no NAT gateway, and DLQs on every queue
    - _Requirements: 2.2, 2.3, 5.1, 5.2, 5.3, 7.4_
  - [x] 6.10 Containers stack (optional until load requires it): ECS Fargate services for the API, chat, job workers, and push consumer using the same image digests, an ALB behind CloudFront, queue-depth autoscaling for workers, CPU autoscaling for HTTP services, and an ECS scheduled task for the sweeper
    - _Requirements: 3.2, 3.9_

- [x] 7. Set up the CI/CD pipeline
  - [x] 7.1 Create `ci.yml`: lint, type check, unit tests, build both images, run the container integration tests with `docker-compose`, run the scale test, coverage summary, and `cdk synth`
    - _Requirements: 8.1, 8.2, 8.4, 8.5, 8.7, 8.8_
  - [x] 7.2 Add a `GithubOidc` CDK stack (OIDC provider and a least-privilege deploy role, deployed once by hand), and create `deploy.yml` that runs on `main` after CI passes: push the tested image digests to ECR, run Alembic migrations as a one-off task, `cdk deploy` with GitHub OIDC, upload the frontend, invalidate CloudFront, and run the Lambda-mode smoke test (including the Data API read and write)
    - _Requirements: 1.3, 1.4, 8.7_

- [x] 8. Add AI call instrumentation
  - Wrap the Anthropic client in a service that logs purpose, model, input and output tokens, cache hits, and latency, and checks the global AI-call rate limit
  - Add `FakeClaude` in `tests/support/ai.py`, replaying recorded responses from `tests/fixtures/ai/`, and a `make record-ai` target that records new ones with the live model
  - Write unit tests for logging, the global limit, and the stub
  - _Requirements: 9.3, 2.4, 8.3_

- [x] 9. Write the scale test
  - Start two instances of each queue consumer and run the sweeper twice concurrently against shared LocalStack queues and PostgreSQL
  - Assert no duplicate datasets, versions, check results, or Exchanges, and no overlapping processing of one dataset
  - _Requirements: 3.1, 3.5, 3.6, 8.8_

- [x] 10. Build the end-to-end test harness
  - Set up the Playwright project, the AI stub toggle, and the fixture-site base URL per environment
  - Write the smoke E2E test: open the app and see the empty dataset library without signing in
  - The full main-flow E2E test is completed in the `guardrailed-chat` spec
  - _Requirements: 2.1, 8.3, 8.6_

- [x] 11. Write the API performance test
  - Add `tests/perf/test_api_latency.py` that seeds 1,000 reviews and asserts p95 latency under 500 ms for the list and summary endpoints against the container integration environment
  - Run it in CI as a separate, non-blocking job that reports the numbers
  - _Requirements: 7.3_

- [x] 12. Write property-based tests for the Correctness Properties
  - Implement one property test per property in the design (Hypothesis), each tagged with its property number
  - _Requirements: 2.3, 2.4, 2.5, 3.4, 3.5, 4.4, 5.1_
- [x] 13. Make the shared DynamoDB test-table setup collision-safe
  - Fix the `ResourceInUseException: Table already exists: rate-limits` failures that break the backend unit and property suites which create the `rate-limits` table under moto (`tests/unit/worker/ai/*`, `tests/unit/extraction/*`, `tests/unit/core/test_ai.py`, `tests/unit/core/test_rate_limit.py`, `tests/property/test_rate_limit.py`, and others). Root cause (verified): the moto backend persists across `@mock_aws`-decorated tests in the same process, so each test's `create_table` collides with a table a prior test created — a test fails even run on its own, and within one module the earliest tests pass while every later one fails.
  - Replace the duplicated per-module `_create_rate_limit_table()` / `_create_table()` helpers with a single shared, idempotent setup (a fixture in `tests/conftest.py`, or an `ensure_table` helper) that either tolerates an already-existing table or guarantees a freshly-reset moto backend per test, so table creation is safe regardless of test order or what a prior test left behind. Keep the real provisioned schema (PK, PAY_PER_REQUEST, TTL) the current helpers use.
  - Apply the same treatment to the other moto-created tables that use the copy-paste pattern (`check-sessions`, `ws-connections`) if they share the same hazard.
  - Confirm `make test` (backend unit + property) runs green from a clean checkout and that no single test file fails in isolation; note any remaining failures that are genuinely about a different cause.
  - _Requirements: 8.1_
- [x] 14. Pin worker image dependencies to `uv.lock` and fix the capture greenlet crash
  - Root cause (verified): `Dockerfile.workers` installs deps with `uv pip install --system "."`, which resolves from the open-ended `pyproject.toml` floors instead of `uv.lock`, so a rebuilt image floats above the locked versions. This is the common cause of `docs/known-issues-live-stack.md` Issues 1 (selectolax floated to 1.0) and 2 (newer Playwright/greenlet surfaced the capture crash). The lockfile already has the known-good versions (playwright 1.63.0, greenlet 3.5.6, selectolax 0.4.13).
  - Make both images install the locked dependency set: copy `uv.lock` into the build context and install with `uv sync --frozen` (or an equivalent locked install) in `Dockerfile` and `Dockerfile.workers`, so the image matches `uv.lock` exactly and no transitive dependency (greenlet included) floats. Keep the selectolax `<1.0` floor from Issue 1 as a belt-and-braces guard.
  - Fix the Playwright `greenlet.error: cannot switch to a different thread` in `app/capture/engine.py`. The sync Playwright API is not thread-safe and the engine caches one `sync_playwright().start()` browser in a module global reused across messages; a browser bound to the thread/greenlet that created it cannot be driven once that context changes (the per-message heartbeat thread in `app/consumer.py::_process_one` and process reuse make this fragile). Make capture own its Playwright lifecycle on the thread that drives `render` — e.g. run each capture in a dedicated worker thread that starts and stops its own `sync_playwright()` instance for its lifetime, or otherwise guarantee the Playwright object is created and used on one thread and never switched. Do not change the handler/consumer contract or import Lambda shapes into capture.
  - Verify: `make lint` and `make test` (the `app/capture` unit tests) pass; `make test-int` for `tests/integration/capture/test_capture_int.py` passes against the Compose stack; and a live `make up` capture of a fixture page writes `page.html` + `snapshot.png` with no `greenlet.error` in `docker compose logs workers-check`. If the Compose/live parts can't run in this environment, run what you can and report what was and wasn't verified.
  - Do NOT fix Issue 3 (dataset-ingestion) here — it is a separate spec and may be a downstream symptom; this task stops at a working capture.
  - _Requirements: 3.2, 3.3_
- [x] 15. Fix the enum/UUID bind-type mismatch in the application SQL (integration suite)
  - Root cause (VERIFIED on a freshly recreated Postgres volume, so this is NOT stale state and NOT caused by task 14's Docker/capture change): raw SQL binds plain Python `str` values for the native `dataset_status` enum and `uuid` columns, so a stricter psycopg/SQLAlchemy (pulled in by the dependency rebuild) emits `status = %(status)s::VARCHAR` and `str`→`uuid` binds and the server rejects them — `operator does not exist: dataset_status = character varying` and `column "id" is of type uuid but expression is of type character varying`. ORM-based inserts/queries are fine; only the raw-SQL paths break. 37 integration failures + 12 errors across the sweeper, ingestion add/upload service, and the processing/collection/isolation handlers all trace to this.
  - Fix the binds so enum and uuid columns are compared/inserted with the correct types — the smallest correct change (e.g. typed `bindparams` with `sqlalchemy.Uuid` / the `dataset_status` enum type, or explicit `:param::dataset_status` / `:param::uuid` casts) — in the app modules the failing tests exercise: `app/jobs/sweep.py` (`SELECT ... WHERE status = :status ... FOR UPDATE SKIP LOCKED` and the version-claim/update queries) and `app/ingestion/service.py` (`_insert_dataset`-style `INSERT INTO datasets (...)` / `dataset_versions (...)` in both the URL and upload paths, plus the status/claim queries in `create_from_check`). Grep for sibling raw SQL with the same pattern and fix those too. Behaviour must be identical — this is a bind-typing fix, not a logic change. Do not switch these paths to the ORM.
  - Cross-spec note: `sweep.py` and `ingestion/service.py` are nominally dataset-ingestion/review-analysis files, but this is one shared data-layer (platform-foundation) defect, so it is fixed here deliberately; keep the change limited to the typed binds.
  - Verify against a FRESH DB volume (`docker compose rm -sfv postgres && docker compose up -d postgres`) that `make test-int` is green for the previously-failing suites: `tests/integration/ingestion/test_add_service_int.py`, `tests/integration/ingestion/test_upload_service_int.py`, `tests/integration/jobs/test_sweep_int.py`, `tests/integration/handlers/test_processing_pipeline_int.py`, `tests/integration/handlers/test_collection_int.py`, `tests/integration/handlers/test_isolation_int.py`. Also confirm `make lint` and the unit/property suites stay green (1443 passed) and the capture integration tests still pass.
  - PROGRESS (this session): the APP-CODE casts are DONE and verified — `app/jobs/sweep.py` and `app/ingestion/service.py` now use `CAST(:id AS uuid)` / `CAST(:status AS dataset_status)` / `CAST(:source_type AS source_type)`, and the add/sweep/upload service tests went from all-failing (enum/uuid errors) to passing (bar an unrelated origin-guard 403 and one concurrency-timing assertion). REMAINING: the SAME `str`→`uuid`/enum pattern also lives in the integration-test FIXTURE SQL (seed INSERT, teardown DELETE, SELECT/UPDATE `WHERE id = :id`) across `tests/integration/handlers/test_processing_pipeline_int.py`, `test_collection_int.py`, `test_isolation_int.py`, `test_check_handler_int.py`, `test_check_handler_refresh_origin_int.py`, `tests/integration/ingestion/test_upload_service_int.py`, `tests/integration/datasets/test_refresh_int.py`, `test_archive_int.py`, `tests/integration/chat/*`, and siblings — these must get the same `CAST(... AS uuid)` / enum casts so `make test-int` is green. This is the test harness (platform-foundation Req 8.2), fixed here deliberately; identical bind-typing fix, no logic change.
  - Then confirm whether this clears Issue 3 in `docs/known-issues-live-stack.md` (Add returns `created` but inserts no dataset) — it is very likely the same root cause (the insert was being rolled back by this error). Re-run the Issue 3 repro and record the result; if a genuinely separate dataset-ingestion bug remains, document it rather than fixing it here.
  - _Requirements: 8.2_
- [x] 16. Stamp status-event `at` under the row lock (Property 5 time-order) — see design.md → Known Issues
  - The scale test `tests/scale/test_concurrency_invariants.py::test_concurrent_status_writes_are_lossless_and_ordered` fails: concurrent `log_event`/`transition` on one dataset keep every event (lossless, verified) but their `at` timestamps can be out of order (~8 ms inversion), violating Correctness Property 5 ("events SHALL be in time order"). Root cause: `app/db/status.py::_build_event` stamps `at` via `_utc_now_iso()` BEFORE `_append_event` takes `SELECT ... FOR UPDATE`, so the array is ordered by commit, not by `at`.
  - Fix: assign `at` INSIDE the lock at append time so timestamp order always matches append order — e.g. move the `at` stamp into `_append_event` after acquiring the lock, or set it in the append `UPDATE` using the row-locked transaction's clock. Keep the event shape (`status`, `at`, `message`, `data`), keep `transition` publishing exactly one `dataset.status.changed` per call, and keep "the last status event equals the `status` column". The published event payload's `at` must match the stored event's `at`.
  - Verify (consumers can be up or down; this is DB-only): `tests/scale/test_concurrency_invariants.py` passes (the ordered-and-lossless test green), `tests/integration/db/test_status.py` stays green, `make lint`, and the unit/property suite stays green (`tests/property/test_status_history.py` included).
  - _Requirements: 4.4_
- [ ] 17. Make the live E2E able to seed a dataset locally (fixture host / SSRF mismatch)
  - The Playwright E2E suite skips its 9 pipeline tests (`needs-record-ai`) because `seedDataset` cannot create a dataset. Verified root cause: the E2E addresses the fixtures site as `http://localhost:9090` (correct for a host browser), but the containerized backend's `SSRF_TEST_ALLOW_HOSTS=fixtures` (repo `.env`) only permits the Docker-internal host `http://fixtures`, so a Check for the E2E URL returns `wont_work: "Address not allowed"` and seeding yields nothing. The pipeline itself is proven working: a Check for `http://fixtures/extraction/plain_list/` returns `limited` and Add(`confirm_limited`) creates a dataset row (this also confirms the original Issue 3 "Add inserts nothing" is resolved).
  - Fix (test/compose/config side only — do NOT weaken product SSRF): make the host the browser uses and the host the backend fetches reconcile under the local stack. Options to evaluate: (a) add `localhost` (and `localhost:9090`'s host) to the local E2E `SSRF_TEST_ALLOW_HOSTS` so the backend accepts the browser-facing fixture URL; and/or (b) set `E2E_FIXTURE_BASE_URL` to a host the backend can both resolve and is allowlisted (mirroring the Issue-4 registrable-domain approach, e.g. a compose alias). Ensure the Check actually renders the fixture (capture needs to reach the fixtures container from inside the Docker network). Keep production behavior unchanged (prod points at the real public CloudFront fixtures URL; `SSRF_TEST_ALLOW_HOSTS` must stay refused when `ENV=production`).
  - Verify: with the full stack up and worker consumers RUNNING (E2E needs the real pipeline), `make e2e` (AI stubbed) runs the 9 currently-skipped tests to green instead of skipping — or, where a test genuinely needs a recorded Locator response, document precisely which and why. Do not require `ANTHROPIC_API_KEY` for the stubbed run.
  - _Requirements: 8.3, 8.6_

- [x] 18. Pin the Aurora engine to an available version (unblock the Data stack deploy) — see design.md → Known Issues
  - Root cause (found by the first live `cdk deploy ReviewLens-Data`): `infra/lib/data-stack.ts` pinned `AuroraPostgresEngineVersion.VER_16_4`, but AWS removed the plain `16.4` minor from `aurora-postgresql` (only `16.4-limitless` remains), so cluster creation failed with `Cannot find version 16.4 for aurora-postgresql` and the stack rolled back. This blocks the whole deploy since every other stack depends on Data.
  - Fix: pin to `VER_16_8` — the lowest still-available standard 16.x in us-east-1 (`aws rds describe-db-engine-versions --engine aurora-postgresql` listed 16.8/16.9/16.10/16.11/16.13/16.14/16.15). Product intent unchanged: Aurora PostgreSQL 16, Serverless v2, Data API. No test asserts the version string. Prefer the lowest in-support 16.x over the newest, since AWS keeps deprecating minors.
  - Also clean up any Retain-policy leftovers a rolled-back attempt orphaned (the `check-sessions`/`rate-limits`/`ws-connections` tables and the stuck stack shell) before re-deploying.
  - Verify: `cdk synth ReviewLens-Data` builds; `cdk deploy ReviewLens-Data` creates the cluster + secrets + tables to `CREATE_COMPLETE`; CDK assertion tests stay green. DONE this session — Data stack deployed, Aurora 16.8 up, Data API enabled.
  - _Requirements: 6.1, 7.1_

- [x] 19. Make the automated deploy assume the OIDC role (`workflow_run` subject) — see design.md → Known Issues
  - Root cause (first automated deploy): CI passed and triggered Deploy, which failed at "Configure AWS credentials (OIDC)" with `Not authorized to perform sts:AssumeRoleWithWebIdentity`. Role ARN, OIDC provider, and `aud` all matched; the `sub` condition rejected the token. `deploy.yml` runs on `workflow_run` (not `push`), so GitHub does NOT present `repo:danjger/ReviewLens-AI:ref:refs/heads/main`, and the trust policy's two allowed subjects didn't match.
  - Fix (config only, no IAM change): add `environment: production` to the deploy job so the OIDC `sub` becomes `repo:OWNER/REPO:environment:production`, which the GithubOidc trust policy already allows via `repo:OWNER/REPO:environment:*`; and create the `production` environment in the repo (`gh api --method PUT .../environments/production`). Keep the trust policy least-privilege and unchanged.
  - Verify: push to `main` → CI green → Deploy assumes the role past the OIDC step and proceeds to `cdk deploy`. DONE this session for the OIDC step; downstream deploy steps verified separately.
  - UPDATE: environment gating alone was NOT sufficient. Live debug showed the real subject is `repo:danjger@984525/ReviewLens-AI@1398576857:environment:production` — GitHub immutable subject claims embed numeric owner/repo IDs. Final fix: broadened the trust `sub` patterns to `repo:danjger*/ReviewLens-AI*:ref:refs/heads/main` and `...:environment:*` (the `*` covers the `@<id>` suffix), applied to the live role and to `github-oidc-stack.ts`. Verified the deploy assumes the role after this change.
  - _Requirements: 1.3, 1.4, 8.7_

- [x] 20. Cross-build the arm64 image assets in the deploy workflow (QEMU) — see design.md → Known Issues
  - Root cause (first CDK deploy): `docker build ... --platform linux/arm64` failed with `exec /bin/sh: exec format error`. The Lambdas/ECS tasks are arm64 (Graviton), so CDK builds arm64 image assets, but the x86_64 `ubuntu-latest` runner had Buildx without QEMU, so arm64 layers couldn't run.
  - Fix: add `docker/setup-qemu-action@v3` (platforms: arm64) before `setup-buildx-action` in `deploy.yml`. CI's `build-images` builds natively (no arm64 pin) so it was unaffected.
  - Verify: deploy's `CDK deploy (Lambda mode)` builds the image assets past the arm64 step. DONE this session (committed; next deploy validates downstream stacks).
  - _Requirements: 1.3, 8.7_

- [x] 21. Adopt mypy 2.x: remove redundant boto3-client casts (CI lint)
  - Root cause: `mypy>=1.13.0` (open floor) resolved to mypy 2.3.1 in CI, which flagged 7 `cast("<Client>", boto3.client(...))` calls as `[redundant-cast]` — the current boto3-stubs type `boto3.client("<svc>")` directly, so the casts are redundant. Lint went red (`make lint` → mypy) with no product change behind it.
  - Fix (adopt the newer, stricter mypy rather than pin): drop the redundant client casts in `app/storage/s3.py`, `app/realtime/connections.py`, `app/events/publisher.py`, `app/core/rate_limit.py`, `app/core/queue.py`, `app/handlers/push.py`, `app/ingestion/check_session.py`, removing the now-unused `cast` import where it was the only use (kept in `check_session.py`, which still casts elsewhere). For the two client factories that then tripped `[no-any-return]`, assign to an annotated local (`client: DynamoDBClient = boto3.client(...)`) and return it — keeps the type without a cast. Cross-spec note: these 7 files span several specs but this is one shared dev-tooling (mypy) drift, fixed here deliberately; behaviour unchanged.
  - Verify: `make lint` green against mypy 2.3.1 (ruff + format + `Success: no issues found in 94 source files`), frontend eslint/tsc green. `uv.lock` unchanged. DONE this session.
  - _Requirements: 8.1_

- [x] 22. Set DLQ visibility timeout >= consumer Lambda timeout (deploy) — see design.md → Known Issues
  - Root cause (first Workers deploy): the three DLQ event-source mappings failed with `Queue visibility timeout: 30 seconds is less than Function timeout: 300 seconds`. The DLQs had no `visibilityTimeout` (default 30s) while the DLQ consumer Lambda is 300s; AWS requires queue visibility >= function timeout for an SQS event source. Synth tests didn't catch it (runtime-only validation).
  - Fix: add `visibilityTimeout: workerVisibility` to `CheckDlq`/`ProcessingDlq`/`PushDlq` in `api-stack.ts`; mirror on `containers-stack.ts` for parity. Delete the terminal `ROLLBACK_COMPLETE` Workers stack so the redeploy recreates it.
  - Verify: 29 synth tests across api/workers/containers stacks pass; the Workers stack creates its DLQ event-source mappings. DONE this session (committed; next deploy validates).
  - _Requirements: 1.3, 8.7_

- [x] 23. Retry migrations past Aurora Serverless v2 auto-pause cold start (deploy) — see design.md → Known Issues
  - Root cause (first deploy to reach migrations; all stacks already up): `alembic upgrade head` failed with `DatabaseResumingException` — Aurora (minCapacity 0 ACU) had auto-paused during the long deploy and the first Data API call hit it mid-resume. Expected cold-start, not a bug.
  - Fix: wrap `alembic upgrade head` in a bash `until` retry (12 attempts × 10s) in `deploy.yml`, so a cold start waits for the cluster to wake. Warms the DB for the smoke test too.
  - Verify: deploy's migration step completes; `alembic upgrade head` reaches head. DONE this session (committed; next deploy validates).
  - _Requirements: 1.3_

- [x] 24. Fix /readyz for Data API mode (smoke test) — see design.md → Known Issues
  - Root cause (post-deploy smoke test; all stacks/migrations/SPA already succeeded): `/readyz` returned 503 "DATABASE_URL not configured". `_check_database` required a psycopg `DATABASE_URL`, but AWS mode uses the RDS Data API and leaves `DATABASE_URL` unset, so readiness always failed on Lambda despite a reachable DB (/healthz 200, Data API read/write ok).
  - Fix: make `app/core/health._check_database` mode-aware — in `settings.is_aws` run `SELECT 1` via the shared engine (Data API), else the psycopg path. `readiness()` signature unchanged so API + chat both get it. Added 2 unit tests for the AWS-mode branches.
  - Verify: `tests/unit/core/test_health.py` green (13 passed); lint/mypy clean; the deploy smoke test's `/readyz` returns 200. DONE this session (committed; next deploy validates the live smoke test).
  - _Requirements: 8.7_

- [x] 25. Close the LocalStack provisioning race that flaked CI scale/integration — see design.md → Known Issues
  - Root cause: `docker compose up -d --wait` intermittently failed because `push-consumer` polled an SQS queue before `infra/localstack-init/01-provision.sh` created it (`QueueDoesNotExist`) and crash-exited. LocalStack's healthcheck reported "running" before the init script finished, so `service_healthy` fired too early.
  - Fix (compose only): init script `touch /tmp/localstack-ready` as its final step; LocalStack healthcheck requires `"running"` AND that sentinel (retries 40×5s). Dependents waiting on `service_healthy` now start only after queues/tables/bus exist.
  - Verify: `make test-scale` from a fresh `-v` volume → all containers Healthy, push-consumer RestartCount 0, exit 0. DONE this session.
  - _Requirements: 8.2, 8.8_

- [x] 26. Retry at the data layer on Aurora auto-pause cold start (runtime 500s) — see design.md → Known Issues
  - Root cause (live E2E): `GET /api/datasets` returned 500 — `DatabaseResumingException` from the Data API when the first request after idle hit a resuming Serverless v2 cluster (minCapacity 0). Affects every cold request, not just migrations; `/readyz` masked it by warming the cluster itself.
  - Fix: `app/core/db` registers an AWS-mode `engine_connect` warm-up that runs `SELECT 1` and retries on resume (12×5s) before any real statement — covering API, chat, and workers. Only the connection warm-up retries (no business-logic re-execution). Added 4 unit tests (install-only-in-aws, retry-then-succeed, give-up, non-resume reraise).
  - Verify: `tests/unit/core/test_db.py` green (22 passed); lint/mypy clean; live `GET /api/datasets` returns 200 after a cold start. DONE this session (committed; live re-verify after deploy).
  - _Requirements: 4.1, 7.1_

- [x] 27. Run worker Lambdas under the Lambda RIC, not the web adapter (dead pipeline) — see design.md → Known Issues
  - Root cause (live E2E; Check stuck `pending`): all 5 worker Lambdas (check/processing/push/dlq/sweeper) crashed at init with `Runtime.InvalidEntrypoint`. The images bake in the Lambda Web Adapter (right for the HTTP api/chat), but workers are configured with `cmd=app.consumer.lambda_entry` — a native handler LWA can't dispatch. HTTP tier healthy, entire background pipeline dead; smoke test didn't exercise a queue consumer.
  - Fix: add `awslambdaric` to runtime deps; in `workers-stack.ts` set each worker's container `entrypoint` to the RIC (`python -m awslambdaric`) and blank `AWS_LAMBDA_EXEC_WRAPPER` to disable LWA. One image, both modes preserved; api/chat unchanged; container poller unchanged.
  - Verify: `cdk synth` + 8 workers synth tests pass; synthesized template shows RIC EntryPoint + blank exec wrapper on all five; `app.consumer.lambda_entry` imports. DONE this session; live pipeline re-verified after rebuild/redeploy.
  - UPDATE: RIC fixed InvalidEntrypoint but exposed a 10s INIT timeout — the LWA binary under /opt/extensions/ loads as an extension at init regardless of the exec-wrapper env, eating init time on the heavy image. Removed the LWA COPY from Dockerfile.workers and pointed all 5 workers at that LWA-free image (push/dlq/sweeper moved off the base Dockerfile). Handlers import lazily so RIC init stays light.
  - _Requirements: 1.1, 1.2, 7.4_

- [x] 28. Install chrome-headless-shell in the workers image (capture launch crash) — see design.md → Known Issues
  - Root cause (first full render, Judge.me): `launch(headless=True)` crashed — Playwright 1.63 uses the separate `chrome-headless-shell` binary, which `playwright install chromium` doesn't include; item stuck `checking` on retry.
  - Fix: `python -m playwright install chromium chromium-headless-shell` in `Dockerfile.workers`.
  - Verify: image rebuild deploys; a renderable Check reaches a non-crash capture (title/reviews populated or a real content-based verdict). DONE (code); live re-verify after deploy.
  - _Requirements: 3.1_

- [x] 29. Install Playwright browsers to a world-readable path (Lambda non-root) — see design.md → Known Issues
  - Root cause: with the headless shell installed and present in the image, the Lambda still said "Executable doesn't exist" — browsers lived under `/root/.cache` (mode 700) and the non-root Lambda runtime user can't traverse `/root`. The `PLAYWRIGHT_BROWSERS_PATH=/root/.cache` ENV was also set AFTER the install, so it didn't steer anything.
  - Fix: in `Dockerfile.workers` set `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright` BEFORE `playwright install chromium chromium-headless-shell`, then `chmod -R a+rX /opt/ms-playwright`.
  - Verify: built the image and ran `test -x <chrome-headless-shell>` as uid 1051 → executable. Live: a renderable Check now launches the browser and renders. DONE (code + local proof); live re-verify after deploy.
  - _Requirements: 3.1_

- [x] 30. Supply the FIFO dedup id on every processing-queue send — see design.md → Known Issues
  - Root cause (live upload): SQS SendMessage to the processing FIFO queue failed — the deployed queue has content-based dedup OFF (api-stack.ts, by design) but all four producers enqueued with only `message_group_id`, no `MessageDeduplicationId`. Local LocalStack has content-based dedup ON, which hid it.
  - Fix: `ingestion/service.py` (URL add + upload), `datasets/refresh_service.py`, `jobs/sweep.py` now pass `message_deduplication_id=f"{dataset_id}:{version}"`. Cross-spec (dataset-ingestion/dataset-library/review-analysis), one shared defect.
  - Verify: ruff/mypy clean; add-service unit test asserts the dedup id; live upload submit succeeds (dataset reaches processing). DONE (code); live re-verify after deploy. Follow-up: align the local provision script's FIFO dedup setting with prod.
  - _Requirements: 1.3_

- [x] 31. Cast id to uuid in db.status._append_event (Data API) — see design.md → Known Issues
  - Root cause (first prod processing): `_append_event` ran `SELECT 1 FROM datasets WHERE id = :id FOR UPDATE` (and a jsonb_set UPDATE) binding `id` as text; the Data API driver made it `uuid = text` → `ER_UNDEF_FUNC`. A stale comment claimed the params-dict bind stays `uuid = uuid` (only true on local psycopg). Task 15 missed status.py; integration tests on local psycopg didn't catch it.
  - Fix: `CAST(:id AS uuid)` in both `_append_event` queries. All status writes go through it, so every transition/log_event is unblocked in Data API mode.
  - Verify: status unit (8) + property (1) tests green; live dataset reaches `updated` after deploy. DONE (code); live re-verify after deploy.
  - _Requirements: 8.2_

- [x] 32. Grant every worker read on the app secrets (config-load crash) — see design.md → Known Issues
  - Root cause: all workers set SECRETS_ARN/ORIGIN_VERIFY_SECRET_ARN and `app.core.config` reads both at startup, but only check/processing were granted read. push/dlq/sweeper AccessDenied at config load (GetSecretValue on the Anthropic secret) and never started — so dead-lettered datasets were never marked failed and the sweeper never ran.
  - Fix: grant `anthropicSecret`/`originVerifySecret` read inside `grantCoreData` (used by every worker); remove the duplicate explicit grants on check/processing.
  - Verify: synth template shows GetSecretValue on all 5 workers; 8 workers synth tests pass. Live: dlq-consumer/sweeper start cleanly after deploy. DONE (code); live re-verify.
  - _Requirements: 6.1, 7.4_

- [x] 33. Pin BuildKit to the GCR mirror in CI/deploy (Docker Hub pull flake) — see design.md → Known Issues
  - Root cause: `setup-buildx-action` pulled `moby/buildkit` from registry-1.docker.io, which timed out repeatedly on the runner ("context deadline exceeded"), failing the build-images job across re-runs.
  - Fix: `driver-opts: image=mirror.gcr.io/moby/buildkit:buildx-stable-1` in ci.yml and deploy.yml (keeps docker-container driver + type=gha cache).
  - Verify: GCR mirror serves the tag; CI build-images goes green. DONE.
  - _Requirements: 8.7_

- [x] 34. Add S3 CORS so browser CSV uploads work (UI upload blocked) — see design.md → Known Issues
  - Root cause (live UI): the Upload tab PUTs the file browser→S3 pre-signed URL (cross-origin from CloudFront); the bucket had no CORS rule, so the preflight was blocked ("No Access-Control-Allow-Origin"). API/curl path doesn't preflight, so it passed while the UI failed.
  - Fix: `cors` on AppBucket — PUT from `https://*.cloudfront.net` (+ localhost), AllowedHeaders *, ExposedHeaders ETag. Objects stay private; only scopes page origins. Pattern origin avoids a circular dep with Edge.
  - Verify: data-stack synth test asserts the CORS rule (added); live browser upload completes after the Data stack redeploys.
  - _Requirements: 7.1_

- [ ] 35. Route /realtime through CloudFront to the WebSocket API (live updates in prod) — see design.md → Known Issues
  - Root cause (live browser): `wss://<cf-domain>/realtime` handshake fails with "Unexpected response code: 200" — CloudFront serves the SPA/HTTP origin for `/realtime` instead of upgrading to the API Gateway WebSocket (RealtimeStack). Live push updates don't work in the deployed app.
  - Fix: add a CloudFront behavior/origin for `/realtime` → the WebSocket API stage with WS upgrade handling (Edge/Realtime CDK). Keep the HTTP `/api/*` behavior unchanged.
  - Verify: a live check/process updates a tracked row without a reload against the deployed stack. NOT started.
  - _Requirements: (realtime push)_

- [x] 36. Stop CloudFront rewriting API 404s to the SPA — see design.md → Known Issues
  - Root cause: distribution-wide `errorResponses` (403/404 → /index.html 200) caught API 4xx too, so `/api/.../snapshot-url` 404 returned HTML and the UI showed a JSON parse error.
  - Fix: remove the global error responses; add a CloudFront viewer-request Function on the default SPA behavior that rewrites only extension-less non-/api navigation to /index.html. API 4xx pass through.
  - Verify: 73 CDK tests pass + `cdk synth ReviewLens-Edge` OK; after deploy, `/api/.../snapshot-url` for an upload returns JSON 404 via CloudFront (not HTML).
  - _Requirements: 1.3_
