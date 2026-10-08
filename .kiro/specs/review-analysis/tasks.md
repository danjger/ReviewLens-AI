# Implementation Plan

- [x] 1. Build the processing handler skeleton and start stage
  - [x] 1.1 Implement `handlers.processing` on the shared consumer runtime, wired to the FIFO `processing-queue` (group = dataset ID, dedup = `dataset_id:version`), with DLQ, 3 retries, a 15-minute time budget, and a visibility timeout of 6× the Lambda timeout
    - _Requirements: 1.3, 7.1_
  - [x] 1.2 Implement the start guard: continue on `requested` or on a retry of the same version in `processing`; skip superseded, completed, or archived versions
    - Write unit tests for each branch and a property test for Property 6
    - _Requirements: 1.1, 1.2, 8.1_
  - [x] 1.3 Implement the sweeper job (`SKIP LOCKED` claims): re-enqueue stale `requested` rows, fail stale `processing` rows; and the DLQ consumer that marks exhausted messages `failed`
    - Write integration tests with backdated rows, two concurrent sweeper runs, and a DLQ message
    - _Requirements: 1.4, 1.5, 7.2_

- [x] 2. Implement the page collection stage
  - Loop with `extraction.next_page()` and `capture.render()`, with SSRF checks, per-host delay, and the MAX_PAGES, MAX_REVIEWS, and time-budget limits
  - Skip pages already captured for this version
  - Append a progress event for each page; on a page failure, stop and record a warning; record the script-only pagination warning
  - Skip this stage for upload datasets
  - Write unit tests for every loop exit and an integration test with fixture pages
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 7.3_

- [x] 3. Implement the extraction stage
  - [x] 3.1 Call `extraction.extract_page()` for each captured page with the saved plan and collect per-page details
    - _Requirements: 3.1, 3.2_
  - [x] 3.2 Implement upload extraction from `mapping.json`, counting skipped empty rows and applying the MAX_REVIEWS keep rule with a warning
    - _Requirements: 3.4_
  - [x] 3.3 Implement dedupe across pages
    - _Requirements: 3.3_
  - [x] 3.4 Write property tests for Properties 1 and 2
    - _Requirements: 2.1, 3.3, 3.4_

- [x] 4. Implement AI analysis tasks
  - [x] 4.1 Implement `entity_profile` with the content-only prompt, the plan's entity hint, and the low-confidence fallback to the title or name
    - _Requirements: 4.1, 4.2_
  - [x] 4.2 Implement batched `classify_sentiment` with the rating-based fallback
    - _Requirements: 5.2_
  - [x] 4.3 Implement `extract_themes` with the check that example IDs exist
    - _Requirements: 5.3_
  - [x] 4.4 Add a JSON schema-repair retry for every AI task and re-raise `AIUnavailable`, with unit tests using malformed fixture outputs
    - _Requirements: 7.1, 7.4_

- [x] 5. Implement metrics and output
  - Compute count, average rating, distribution, date range, pages, skipped, reported total, sentiment breakdown, themes, and the extraction details
  - Write `reviews/v{n}.json` with per-page details, overwriting idempotently by version
  - Write unit tests for the metric math, including datasets without ratings or dates, and a property test for Property 3
  - _Requirements: 3.5, 5.1, 5.4, 5.5, 5.6, 7.3_

- [x] 6. Implement completion and failure handling
  - Transition to `updated` with `updated_at`, the active version, metrics, and a completion event; transition to `failed` for zero reviews
  - Detect the final retry and write a user-safe failure message, including the AI-unavailable message
  - Complete the `dataset_versions` row with the extraction method; set `active_version` only on success; record actual results next to the viability prediction
  - Write unit tests and property tests for Properties 4 and 5
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 7.2, 7.4, 8.1_

- [x] 7. Write integration tests for the pipeline (worker running as a container)
  - Test the happy path through the status sequence and published events for a `selectors` dataset with one fallback page, and for an `ai_direct` dataset
  - Test zero reviews ending in `failed`
  - Test a later-page failure ending in `updated` with a warning
  - Test that reprocessing does not create duplicate outputs, and that a retry after a crash doesn't re-capture pages
  - Test AI unavailable through every retry ending in `failed`
  - Test a failed refresh leaving `active_version` and `metrics` unchanged
  - Test the upload dataset path, including an over-limit file
  - _Requirements: 1.1, 1.2, 2.3, 3.2, 3.4, 6.1, 6.2, 6.5, 7.3, 7.4, 8.1_

- [x] 8. Write the isolation test
  - Run extraction and analysis with outbound network blocked (except the AI stub and LocalStack) and assert success
  - _Requirements: 3.1_
- [x] 9. Fix the collection integration tests that stop at page 1 (bare-host fixture)
  - See this spec's `design.md` → `## Known Issues` and `docs/known-issues-live-stack.md` (Issue 4) for the full writeup and repro.
  - Verified root cause (NOT a product regression): `tests/integration/handlers/test_collection_int.py`, `test_processing_pipeline_int.py`, and `test_isolation_int.py` paginate an inline fixture served at the bare host `http://fixtures`. `app.extraction.next_page` only follows a next-page link on the **same registrable domain** (Requirement 5.3); a bare single-label host has no registrable domain per `tldextract`, so `pagination._same_registrable_domain("http://fixtures/a","http://fixtures/b")` is `False`, every `rel="next"`/"Next" candidate is dropped, and collection stops after page 1 with `reason_if_none="script_driven_no_url"`. Confirm with the one-line repro before changing anything.
  - Fix TEST-SIDE only: give the multi-page collection fixtures a host that HAS a registrable domain (e.g. `http://fixtures.test/…` or a `*.example.com` alias resolving to the Compose `fixtures` container) and keep that host on `SSRF_TEST_ALLOW_HOSTS`, so page N and N+1 are same-site and `next_page` advances. Change only the inline `_page_html(...)` builders and `_PAGE_URLS` in those three test files (plus the SSRF allowlist / any Compose DNS alias needed to serve that host). Do NOT relax `_same_registrable_domain` or any `app/extraction/*` product code — that same-site rule is review-extraction's and is a real Requirement 5.3 guarantee. If serving the new host needs a `docker-compose.yml` change (platform-foundation's file), surface it rather than editing silently.
  - Verify with the worker CONSUMERS STOPPED (otherwise the live Compose containers drain the SQS messages the tests assert on → spurious `_drain_processing_queue() == []`): recreate a fresh Postgres volume, `docker compose stop workers-check workers-processing push-consumer sweeper`, then run the three suites to green. Also confirm `make lint` and the unit/property suite stay green. Report the host/alias approach chosen and before/after results.
  - Prerequisite enum/UUID bind-type fix (platform-foundation task 15) is already done; this fixture-host issue is the remaining blocker for a green collection suite.
  - _Requirements: 2.1, 2.2_
- [x] 10. Fix the processing-queue message field mismatch (release-blocking) — see design.md → Known Issues
  - Root cause (found by the live E2E): producers enqueue `{dataset_id, data_version}` (dataset-ingestion `service.py`, `datasets/refresh_service.py`; sweeper), and the DLQ handler already reads `data_version` (with `version` as a synonym) — but `app/handlers/processing.py::ProcessingMessage.from_body` reads `body["version"]`, so every real processing message raises `ValueError: processing message missing 'version'` and datasets stay `requested` forever. The processing unit tests passed only because they build `{"version": n}` bodies — i.e. they encode the wrong contract.
  - Fix: in `ProcessingMessage.from_body`, read the version from `data_version` with `version` accepted as a backward-compatible synonym (mirror `app/handlers/dlq.py::_parse`: `body.get("data_version", body.get("version"))`). Keep the same validation (non-empty str id; positive int version) and the DLQ-on-malformed behavior. `data_version` is the canonical key (producers + DLQ handler + design agree); do not change the producers.
  - Update `tests/unit/handlers/test_processing_handler.py` to use `data_version` as the primary key (keep one test that the legacy `version` synonym still parses), without weakening the validation tests.
  - Add a CONTRACT test that the body a producer actually enqueues parses through `from_body` — e.g. assert `ProcessingMessage.from_body({"dataset_id": "ds-1", "data_version": 1})` succeeds, and ideally a cross-check against the literal dict `service.py`/`refresh_service.py` send — so producer/consumer can never silently diverge again.
  - Verify: `make lint`; unit/property green; the processing integration tests green; and the LIVE E2E (`make e2e`, AI stubbed) with the DB migrated and consumers running — a seeded dataset must reach `status=updated` / `active_version=1` and the previously-failing pipeline E2E tests pass (or, where a test needs a recorded Locator response, document which). Prereq for the live run: Alembic migrations applied to the Compose Postgres (`uv run alembic -c app/db/migrations/alembic.ini upgrade head`).
  - _Requirements: 7.1, 1.1_
