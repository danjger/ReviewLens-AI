# Implementation Plan

- [ ] 1. Build the processing handler skeleton and start stage
  - [ ] 1.1 Implement `handlers.processing` on the shared consumer runtime, wired to the FIFO `processing-queue` (group = dataset ID, dedup = `dataset_id:version`), with DLQ, 3 retries, a 15-minute time budget, and a visibility timeout of 6× the Lambda timeout
    - _Requirements: 1.3, 7.1_
  - [ ] 1.2 Implement the start guard: continue on `requested` or on a retry of the same version in `processing`; skip superseded, completed, or archived versions
    - Write unit tests for each branch and a property test for Property 6
    - _Requirements: 1.1, 1.2, 8.1_
  - [ ] 1.3 Implement the sweeper job (`SKIP LOCKED` claims): re-enqueue stale `requested` rows, fail stale `processing` rows; and the DLQ consumer that marks exhausted messages `failed`
    - Write integration tests with backdated rows, two concurrent sweeper runs, and a DLQ message
    - _Requirements: 1.4, 1.5, 7.2_

- [ ] 2. Implement the page collection stage
  - Loop with `extraction.next_page()` and `capture.render()`, with SSRF checks, per-host delay, and the MAX_PAGES, MAX_REVIEWS, and time-budget limits
  - Skip pages already captured for this version
  - Append a progress event for each page; on a page failure, stop and record a warning; record the script-only pagination warning
  - Skip this stage for upload datasets
  - Write unit tests for every loop exit and an integration test with fixture pages
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 7.3_

- [ ] 3. Implement the extraction stage
  - [ ] 3.1 Call `extraction.extract_page()` for each captured page with the saved plan and collect per-page details
    - _Requirements: 3.1, 3.2_
  - [ ] 3.2 Implement upload extraction from `mapping.json`, counting skipped empty rows and applying the MAX_REVIEWS keep rule with a warning
    - _Requirements: 3.4_
  - [ ] 3.3 Implement dedupe across pages
    - _Requirements: 3.3_
  - [ ] 3.4 Write property tests for Properties 1 and 2
    - _Requirements: 2.1, 3.3, 3.4_

- [ ] 4. Implement AI analysis tasks
  - [ ] 4.1 Implement `entity_profile` with the content-only prompt, the plan's entity hint, and the low-confidence fallback to the title or name
    - _Requirements: 4.1, 4.2_
  - [ ] 4.2 Implement batched `classify_sentiment` with the rating-based fallback
    - _Requirements: 5.2_
  - [ ] 4.3 Implement `extract_themes` with the check that example IDs exist
    - _Requirements: 5.3_
  - [ ] 4.4 Add a JSON schema-repair retry for every AI task and re-raise `AIUnavailable`, with unit tests using malformed fixture outputs
    - _Requirements: 7.1, 7.4_

- [ ] 5. Implement metrics and output
  - Compute count, average rating, distribution, date range, pages, skipped, reported total, sentiment breakdown, themes, and the extraction details
  - Write `reviews/v{n}.json` with per-page details, overwriting idempotently by version
  - Write unit tests for the metric math, including datasets without ratings or dates, and a property test for Property 3
  - _Requirements: 3.5, 5.1, 5.4, 5.5, 5.6, 7.3_

- [ ] 6. Implement completion and failure handling
  - Transition to `updated` with `updated_at`, the active version, metrics, and a completion event; transition to `failed` for zero reviews
  - Detect the final retry and write a user-safe failure message, including the AI-unavailable message
  - Complete the `dataset_versions` row with the extraction method; set `active_version` only on success; record actual results next to the viability prediction
  - Write unit tests and property tests for Properties 4 and 5
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 7.2, 7.4, 8.1_

- [ ] 7. Write integration tests for the pipeline (worker running as a container)
  - Test the happy path through the status sequence and published events for a `selectors` dataset with one fallback page, and for an `ai_direct` dataset
  - Test zero reviews ending in `failed`
  - Test a later-page failure ending in `updated` with a warning
  - Test that reprocessing does not create duplicate outputs, and that a retry after a crash doesn't re-capture pages
  - Test AI unavailable through every retry ending in `failed`
  - Test a failed refresh leaving `active_version` and `metrics` unchanged
  - Test the upload dataset path, including an over-limit file
  - _Requirements: 1.1, 1.2, 2.3, 3.2, 3.4, 6.1, 6.2, 6.5, 7.3, 7.4, 8.1_

- [ ] 8. Write the isolation test
  - Run extraction and analysis with outbound network blocked (except the AI stub and LocalStack) and assert success
  - _Requirements: 3.1_
