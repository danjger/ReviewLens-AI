# Requirements Document

## Introduction

ReviewLens AI is a web-based Review Intelligence Portal for an Online Reputation Management (ORM) consultancy. Analysts use it to ingest the reviews for products or entities from public review platforms, then question that data through a guardrailed Q&A interface.

The app has **no sign-in and no user accounts**. Everyone who opens it sees the same datasets, the same check results, and the same shared Q&A history. Because it is open, it relies on network-level and application-level limits to control abuse and AI cost.

This spec covers the shared foundation that every other feature depends on: hosting and deployment, horizontal scalability, public-access protections, the relational data store, object storage, configuration and secrets, cost limits, testing and CI, and observability. The other specs are `review-extraction`, `dataset-ingestion`, `review-analysis`, `dataset-library`, `ingestion-summary`, and `guardrailed-chat`. All of them build on this one, so implement it first.

## Glossary

- **Portal**: The ReviewLens AI web application: frontend, HTTP services, and background workers.
- **Dataset**: One ingested set of reviews, from a URL or an uploaded file, identified by a unique ID.
- **Data Store**: The Aurora PostgreSQL database that holds dataset records.
- **Object Store**: The S3 bucket that holds raw page captures, snapshots, extraction plans, extracted reviews, and chat history.
- **Service**: A deployable unit of the backend: the API service, the chat service, the job workers, and the sweeper.
- **Compute mode**: Where Services run: AWS Lambda (the initial deployment) or containers (for example ECS Fargate).
- **status_detail**: A JSON column on each dataset record that holds an ordered log of processing events with timestamps.

## Requirements

### Requirement 1: Public hosting and deployment

**User Story:** As a reviewer of this project, I want the Portal hosted at a public URL and deployable from source, so that I can use it and reproduce the deployment.

#### Acceptance Criteria

1. THE Portal SHALL be reachable over HTTPS at a public URL.
2. THE Portal's infrastructure SHALL be defined as code in the GitHub repository.
3. WHEN a developer runs the documented deploy command with valid cloud credentials, the system SHALL provision or update all infrastructure and deploy the frontend and every Service.
4. WHEN a commit is merged to the `main` branch, the CI pipeline SHALL run all tests and, only if they pass, deploy to production.
5. THE repository SHALL include a README that covers local setup, environment variables, deployment, compute modes, cost controls, and architecture.

### Requirement 2: Open access with abuse protection

**User Story:** As the consultancy, I want the app open to anyone with the link without letting a single visitor run up AI or scraping costs, so that it is easy to use and cheap to run.

#### Acceptance Criteria

1. THE Portal SHALL NOT require sign-in. Every page and API SHALL be usable without credentials, and every visitor SHALL see the same datasets, check results, and Q&A history.
2. ALL public traffic SHALL enter through a single CDN distribution protected by a web application firewall with a per-IP request rate rule.
3. THE backend HTTP Services SHALL reject requests that did not come through the CDN.
4. THE system SHALL enforce configurable rate limits on URL checks, uploads, refreshes, and chat questions, both per client IP and globally across all visitors, and SHALL return HTTP 429 with a retry time when a limit is exceeded.
5. THE system SHALL NOT store personal data about visitors. Client IPs MAY be used for rate limiting and SHALL NOT be written to dataset records, chat history, or logs except as a one-way hash.

### Requirement 3: Horizontal scalability and compute portability

**User Story:** As the CTO, I want every backend Service to scale out by adding instances, and to run unchanged on Lambda or in containers, so that we can move to containers when load requires it without rewriting code.

#### Acceptance Criteria

1. EVERY Service SHALL be stateless. All shared state SHALL live in the Data Store, the Object Store, DynamoDB, or the message queues. In-process memory MAY be used only as a cache of immutable data, and correctness SHALL NOT depend on it.
2. EVERY Service SHALL be built as a container image. THE same image SHALL run on AWS Lambda and as a long-running container, selected only by configuration.
3. HTTP Services SHALL be standard HTTP applications that run in both compute modes without code changes.
4. ALL background work SHALL be delivered through message queues. EVERY queue consumer SHALL run either as a Lambda event-source handler or as a long-running poller, using the same message-handling code.
5. EVERY message handler SHALL be idempotent, so that duplicate delivery or concurrent instances cannot corrupt data.
6. EVERY scheduled job SHALL be safe to run on more than one instance at once.
7. LONG-RUNNING container Services SHALL provide health and readiness endpoints and SHALL finish or release in-flight work when asked to shut down.
8. THE local development environment SHALL run every Service as a container, proving the container mode works.
9. THE infrastructure code SHALL deploy in Lambda mode by default and SHALL be structured so that a container mode can be added per Service without changing application code.

### Requirement 4: Relational data store

**User Story:** As a developer, I want a single source of truth for dataset records and their status, so that every component sees consistent state.

#### Acceptance Criteria

1. THE Data Store SHALL be Aurora PostgreSQL Serverless v2, accessed through the RDS Data API.
2. THE Data Store SHALL hold a `datasets` table with at least these columns: unique ID, display name, page title, source type (`url` or `upload`), original URL, final URL, normalized URL, normalized final URL, platform, status, `status_detail` (JSON), request date, last updated date, archived timestamp, data version (latest attempted), active version (latest that finished successfully), and summary metrics (JSON).
3. THE status column SHALL only accept `requested`, `processing`, `updated`, or `failed`.
4. WHEN a component changes a dataset's status, it SHALL append an event to `status_detail` with the new status, a UTC timestamp, and a detail message.
5. THE Data Store schema SHALL be managed by versioned migrations kept in the repository.
6. THE Data Store SHALL hold a `dataset_versions` table with one row per data version of each dataset, recording what started it, when it was requested and completed, its review count, and its outcome.

### Requirement 5: Object storage layout

**User Story:** As a developer, I want a predictable object key layout keyed by dataset ID, so that any component can find a dataset's artifacts.

#### Acceptance Criteria

1. THE Object Store SHALL store every permanent artifact for a dataset under the prefix `datasets/{dataset_id}/`. Temporary objects created before a dataset exists (URL check captures and pending uploads) SHALL live under `checks/` and `uploads/`, and SHALL be deleted automatically after 1 day.
2. THE Object Store SHALL block all public access. The Portal SHALL serve objects only through the API or through short-lived pre-signed URLs.
3. THE Object Store SHALL encrypt objects at rest.

### Requirement 6: Configuration and secrets

**User Story:** As a developer, I want secrets kept out of source control, so that API keys and credentials cannot leak.

#### Acceptance Criteria

1. THE system SHALL read the AI provider API key and database credentials from a managed secrets service at runtime.
2. THE repository SHALL NOT contain secrets. It SHALL provide a `.env.example` that lists every required variable.
3. THE system SHALL expose tunable limits as configuration rather than hard-coded values, including: maximum pages per crawl, maximum reviews per dataset, maximum URLs per check, check timeout, viability thresholds, extraction strategy and token budgets, tracking parameters removed during URL normalization, the AI model IDs, the maximum upload size, and the rate limits.

### Requirement 7: Cost and performance

**User Story:** As the consultancy, I want hosting that is inexpensive and fast, so that the prototype is cheap to run and responsive.

#### Acceptance Criteria

1. THE compute components SHALL scale to zero or near-zero cost when idle in Lambda mode.
2. THE static frontend SHALL be served from the CDN.
3. WHILE a dataset holds up to 1,000 reviews, API reads for the dataset list and dataset summary SHALL respond in under 500 ms at the 95th percentile, excluding cold starts and database resume.
4. THE compute components that call the AI provider or fetch external pages SHALL NOT require a NAT gateway or other always-on network component.
5. THE deployment SHALL include a monthly cloud budget alarm, and the AI provider account SHALL have a spend limit set. Both are documented in the README.

### Requirement 8: Testing and continuous integration

**User Story:** As a senior engineer, I want automated unit and integration tests for every part of the system, so that changes can be deployed with confidence.

#### Acceptance Criteria

1. THE repository SHALL contain unit tests for backend modules and frontend components.
2. THE repository SHALL contain integration tests that exercise the Services against a real PostgreSQL database and LocalStack (S3, SQS, DynamoDB, EventBridge), running locally or in CI.
3. THE repository SHALL contain end-to-end tests that cover the main user flow: open the app, check and add a URL, wait for processing, view summary, ask a question, and see the answer in history.
4. WHEN any test fails in CI, the pipeline SHALL stop and SHALL NOT deploy.
5. THE CI pipeline SHALL report test coverage for backend code.
6. THE test environments SHALL serve fixture review pages that the SSRF protection allows: a public static fixture site for tests against deployed stacks, and a test-only host allowlist for local tests. THE system SHALL refuse to start in production with a host allowlist configured.
7. THE CI pipeline SHALL run the integration tests against the Services running as containers, and SHALL run a post-deploy smoke test against the Lambda deployment.
8. THE CI pipeline SHALL include a scale test that runs two instances of each queue consumer against the same queues and asserts that every message is processed exactly once in effect (no duplicate datasets, versions, or Exchanges).

### Requirement 9: Observability

**User Story:** As an operator, I want structured logs and error visibility, so that I can diagnose failed ingestions and AI calls.

#### Acceptance Criteria

1. THE system SHALL write structured JSON logs to standard output that include the dataset ID, a request or message correlation ID, the Service name, and the instance ID where they apply.
2. WHEN an unhandled error occurs in any Service, the system SHALL log it with a stack trace and SHALL return a generic error message to the caller.
3. THE system SHALL log each AI call's purpose, model, token usage, and latency.
