# Implementation Plan

- [ ] 1. Scaffold the repository and tooling
  - Create the `/infra`, `/backend`, `/frontend`, `/e2e`, `/fixtures-site`, and `/evals` directories as laid out in the design
  - Add ruff, mypy, and pytest config for the backend; eslint, tsc, and Vitest for the frontend
  - Add `.env.example` that lists every required variable
  - Add a `Makefile` with the commands listed in `.kiro/steering/tech.md` (`make up`, `make down`, `make lint`, `make test`, `make test-int`, `make test-scale`, `make e2e`, `make eval`, `make synth`)
  - Write a README with local setup, environment variables, deploy steps, compute modes, cost controls, and an architecture summary
  - _Requirements: 1.2, 1.5, 6.2, 7.5_

- [ ] 2. Build the container images and local environment
  - [ ] 2.1 Write `Dockerfile` (backend) and `Dockerfile.workers` (backend plus Playwright and Chromium), both including the AWS Lambda Web Adapter so they run on Lambda and as plain containers
    - _Requirements: 3.2, 3.3_
  - [ ] 2.2 Write `docker-compose.yml` running the API, chat, job workers, push consumer, and sweeper as containers, plus PostgreSQL, LocalStack, and a fixtures container serving `/fixtures-site`
    - _Requirements: 3.8, 8.6_
  - [ ] 2.3 Add `/healthz` and `/readyz` to every HTTP Service and to the consumer runtime (small HTTP listener in container mode)
    - _Requirements: 3.7_

- [ ] 3. Implement shared backend core
  - [ ] 3.1 Implement `core.config.Settings` with all tunable limits and Secrets Manager loading
    - Fail startup when `SSRF_TEST_ALLOW_HOSTS` is set and `ENV=production`
    - Write unit tests for defaults, overrides, a missing secret, and the production allowlist refusal
    - _Requirements: 6.1, 6.3, 8.6_
  - [ ] 3.2 Implement the structured JSON logger with service, instance, correlation, and dataset context
    - Add FastAPI middleware that assigns a correlation ID to each request
    - Write unit tests that assert the log shape
    - _Requirements: 9.1_
  - [ ] 3.3 Implement the global error handler that returns the generic error envelope
    - Write unit tests for handled errors and unhandled exceptions
    - _Requirements: 9.2_
  - [ ] 3.4 Implement `storage.keys` and the S3 helper functions, including the `checks/` and `uploads/` prefixes
    - Write unit tests for every key builder
    - _Requirements: 5.1_
  - [ ] 3.5 Implement `core.origin_guard` middleware
    - Write unit tests for missing, wrong, and correct headers and the health-endpoint exemption
    - _Requirements: 2.3_
  - [ ] 3.6 Implement `core.rate_limit` with DynamoDB fixed-window counters keyed by hashed client IP and globally, returning 429 with `Retry-After`
    - Write unit tests for window rollover, per-IP limits, and global limits
    - _Requirements: 2.4, 2.5_

- [ ] 4. Implement the queue consumer runtime
  - [ ] 4.1 Implement `app.consumer` with the `Handler` protocol, the Lambda SQS adapter (partial batch failures), and the container long-poll loop (visibility heartbeat, graceful `SIGTERM`)
    - _Requirements: 3.4, 3.7_
  - [ ] 4.2 Write unit tests for both adapters calling the same handler, final-attempt detection from the receive count, heartbeat extension, and shutdown mid-message
    - _Requirements: 3.4, 3.5, 3.7_

- [ ] 5. Implement the data layer
  - [ ] 5.1 Implement `core.db` with the Data API driver in AWS and psycopg locally
    - _Requirements: 4.1, 7.4_
  - [ ] 5.2 Create the SQLAlchemy `Dataset` and `DatasetVersion` models and the first Alembic migration
    - Include the status enum constraint, JSONB columns, `data_version` and `active_version`, the normalized URL columns, and indexes (the unique normalized-URL index can land here or in `dataset-ingestion` task 5)
    - _Requirements: 4.2, 4.3, 4.5, 4.6_
  - [ ] 5.3 Implement `db.status.transition()` and `db.status.log_event()` with an atomic update, a `status_detail` append, and an EventBridge publish
    - Write unit tests with moto for EventBridge
    - Write integration tests against PostgreSQL that confirm the event history is kept in order
    - _Requirements: 4.4_

- [ ] 6. Define infrastructure with CDK
  - [ ] 6.1 Data stack: Aurora PostgreSQL Serverless v2 (min 0 ACU, Data API enabled), a private S3 bucket with SSE, public access blocked, and lifecycle rules deleting `checks/` and `uploads/` after 1 day, DynamoDB tables (`rate-limits`, `check-sessions`, `ws-connections`), and Secrets Manager entries
    - _Requirements: 4.1, 5.1, 5.2, 5.3, 6.1, 7.1, 7.4_
  - [ ] 6.2 Edge stack: CloudFront distribution with behaviors for the SPA, `/api/*`, and `/api/chat/*`; AWS WAF with a per-IP rate-based rule and managed common rules; the `X-Origin-Verify` custom origin header
    - _Requirements: 1.1, 2.2, 2.3, 7.2_
  - [ ] 6.3 Api stack in Lambda mode: API service Lambda (container image, no VPC) behind an HTTP API; chat service Lambda with a streaming Function URL; EventBridge bus; `check-queue`, FIFO `processing-queue`, and `push-queue` with DLQs
    - _Requirements: 1.1, 3.2, 7.1, 7.4_
  - [ ] 6.4 Workers stack in Lambda mode: job-worker Lambdas (workers image) with SQS event sources, the push consumer Lambda, the DLQ consumer, and the sweeper Lambda on an EventBridge Scheduler schedule
    - _Requirements: 3.4, 3.6_
  - [ ] 6.5 Organize the CDK code so each Service reads a `computeMode` context value, with `lambda` implemented and `container` wired to the optional Containers stack
    - _Requirements: 3.9_
  - [ ] 6.6 Frontend stack: S3 origin for the SPA
    - _Requirements: 7.2_
  - [ ] 6.7 Cost stack: AWS Budgets monthly alarm with email notification
    - _Requirements: 7.5_
  - [ ] 6.8 Test fixtures stack: public S3 + CloudFront site publishing `/fixtures-site`, deployed only to non-production accounts
    - _Requirements: 8.6_
  - [ ] 6.9 Write CDK assertion tests: bucket encryption, blocked public access, lifecycle rules, WAF attached with a rate rule, origin header configured, no Lambda attached to a VPC, no NAT gateway, and DLQs on every queue
    - _Requirements: 2.2, 2.3, 5.1, 5.2, 5.3, 7.4_
  - [ ]* 6.10 Containers stack (optional until load requires it): ECS Fargate services for the API, chat, job workers, and push consumer using the same image digests, an ALB behind CloudFront, queue-depth autoscaling for workers, CPU autoscaling for HTTP services, and an ECS scheduled task for the sweeper
    - _Requirements: 3.2, 3.9_

- [ ] 7. Set up the CI/CD pipeline
  - [ ] 7.1 Create `ci.yml`: lint, type check, unit tests, build both images, run the container integration tests with `docker-compose`, run the scale test, coverage summary, and `cdk synth`
    - _Requirements: 8.1, 8.2, 8.4, 8.5, 8.7, 8.8_
  - [ ] 7.2 Add a `GithubOidc` CDK stack (OIDC provider and a least-privilege deploy role, deployed once by hand), and create `deploy.yml` that runs on `main` after CI passes: push the tested image digests to ECR, run Alembic migrations as a one-off task, `cdk deploy` with GitHub OIDC, upload the frontend, invalidate CloudFront, and run the Lambda-mode smoke test (including the Data API read and write)
    - _Requirements: 1.3, 1.4, 8.7_

- [ ] 8. Add AI call instrumentation
  - Wrap the Anthropic client in a service that logs purpose, model, input and output tokens, cache hits, and latency, and checks the global AI-call rate limit
  - Add `FakeClaude` in `tests/support/ai.py`, replaying recorded responses from `tests/fixtures/ai/`, and a `make record-ai` target that records new ones with the live model
  - Write unit tests for logging, the global limit, and the stub
  - _Requirements: 9.3, 2.4, 8.3_

- [ ] 9. Write the scale test
  - Start two instances of each queue consumer and run the sweeper twice concurrently against shared LocalStack queues and PostgreSQL
  - Assert no duplicate datasets, versions, check results, or Exchanges, and no overlapping processing of one dataset
  - _Requirements: 3.1, 3.5, 3.6, 8.8_

- [ ] 10. Build the end-to-end test harness
  - Set up the Playwright project, the AI stub toggle, and the fixture-site base URL per environment
  - Write the smoke E2E test: open the app and see the empty dataset library without signing in
  - The full main-flow E2E test is completed in the `guardrailed-chat` spec
  - _Requirements: 2.1, 8.3, 8.6_

- [ ] 11. Write the API performance test
  - Add `tests/perf/test_api_latency.py` that seeds 1,000 reviews and asserts p95 latency under 500 ms for the list and summary endpoints against the container integration environment
  - Run it in CI as a separate, non-blocking job that reports the numbers
  - _Requirements: 7.3_

- [ ] 12. Write property-based tests for the Correctness Properties
  - Implement one property test per property in the design (Hypothesis), each tagged with its property number
  - _Requirements: 2.3, 2.4, 2.5, 3.4, 3.5, 4.4, 5.1_
