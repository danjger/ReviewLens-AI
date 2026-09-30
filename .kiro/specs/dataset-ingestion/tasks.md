# Implementation Plan

- [ ] 1. Implement URL normalization and validation
  - [ ] 1.1 Implement `url_normalizer.normalize()` with a configurable tracking-parameter list
    - Write unit tests for case, `www`, fragment, tracking parameters, query order, and trailing slash
    - _Requirements: 6.1_
  - [ ] 1.2 Implement input parsing: up to 10 lines, invalid-line marking, collapsing duplicates within a batch
    - _Requirements: 1.1, 1.2, 1.4_
  - [ ] 1.3 Implement `assert_public_host()` for SSRF protection, honoring the test-only allowlist
    - Write unit tests for private, loopback, link-local, and metadata IPs, IPv6, the re-check after redirects, and the allowlist
    - _Requirements: 1.3_
  - [ ] 1.4 Implement `probe()` with browser-like headers, manual redirects, a hop limit, loop detection, and hop logging
    - Write integration tests against the fixtures container: 200, 301→200, 302→404, a loop, and more than 10 hops
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6_
  - [ ] 1.5 Implement `robots.check()` with a short timeout and a no-restriction fallback
    - _Requirements: 3.12_

- [ ] 2. Build the in-process Capture module
  - [ ] 2.1 Implement `capture.render()` in the workers image: one reused browser per process with a fresh context per message, SSRF route guard on every browser request, render, one scroll for lazy content, save HTML and a 1280×900 screenshot, read the title, return the main response status and the browser's final URL
    - _Requirements: 1.3, 2.7, 2.8, 3.1, 4.1, 4.3_
  - [ ] 2.2 Write integration tests that render fixture pages: normal page, page loading a resource from `127.0.0.1` (blocked), meta-refresh redirect (recorded), 403 main response, lazy-loaded reviews
    - _Requirements: 1.3, 2.7, 2.8, 4.1, 4.3_

- [ ] 3. Build the viability assessment
  - [ ] 3.1 Implement the rule-based pre-scan for empty shells, challenge pages, and login walls
    - _Requirements: 3.2, 3.5_
  - [ ] 3.2 Implement `viability.assess()`: pre-scan, structured data, Review Locator, verification, selector validation, method choice, and the Extraction Plan (calling `extraction.build_plan()` from `review-extraction`)
    - _Requirements: 3.1, 3.2, 3.3, 3.11_
  - [ ] 3.3 Implement the verdict rules, plain-language reasons, warnings (robots), samples, and the evidence object, with thresholds from configuration
    - _Requirements: 3.4, 3.5, 3.6, 3.7, 3.12_
  - [ ] 3.4 Implement the AI-unavailable fallback (structured data only, verdict at most `limited`)
    - _Requirements: 3.13_
  - [ ] 3.5 Write unit tests for the verdict rules from a table of plan outcomes, and for the fallback
    - _Requirements: 3.4, 3.5, 3.6, 3.13_
  - [ ] 3.6 Add `assess()` to the extraction evaluation suite as a verdict-accuracy column with the 0.90 threshold
    - _Requirements: 3.14_

- [ ] 4. Build Check Sessions and the check handler
  - [ ] 4.1 Create the DynamoDB `check-sessions` item layout (items as a map for per-item conditional updates) and the `check-queue` consumer wiring
    - _Requirements: 4.4_
  - [ ] 4.2 Implement `handlers.check.CheckHandler`: conditional claim, probe, capture to the check prefix, client-redirect handling, assess, duplicate lookup, write the item and plan, publish `check.updated`; enforce `CHECK_TIMEOUT_S`; mark `error` on the final attempt
    - _Requirements: 2.2, 2.4, 2.7, 2.8, 3.1, 3.10, 6.2, 6.3_
  - [ ] 4.3 Add the `POST /ingest/checks`, `GET /ingest/checks/{id}`, and per-item retry endpoints with per-IP and global rate limiting
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.6, 3.10_
  - [ ] 4.4 Write integration tests with the AI stub: each fixture ends in the expected item state, verdict, plan, and event; a crashed handler leaves the item in `error` and retry works; the same message delivered twice produces one result; 429 after the limit
    - _Requirements: 1.6, 3.1, 3.10_

- [ ] 5. Add the data model changes
  - Write a migration adding `normalized_url` (unique partial index for URL datasets), `normalized_final_url` (indexed), and the `dataset_versions` table with `extraction_method`
  - _Requirements: 5.1, 6.2, 6.7_

- [ ] 6. Build the Refresh Service (shared with dataset-library)
  - [ ] 6.1 Implement `refresh_service.refresh()` with a required capture: guard for already refreshing, restore when archived, a transactional version increment, copy the capture and plan, insert a `dataset_versions` row with the trigger, add the `refresh_requested` event, enqueue
    - _Requirements: 6.4, 6.5, 6.6, 6.8_
  - [ ] 6.2 Implement refresh-origin handling in the check handler: automatic refresh, wait for confirmation, or failed-refresh event with no version row
    - _Requirements: 6.9_
  - [ ] 6.3 Write unit and integration tests for each branch, including two concurrent refresh calls producing one version
    - _Requirements: 6.4, 6.5, 6.6, 6.8, 6.9_

- [ ] 7. Implement Add
  - [ ] 7.1 Implement `add_items()`: refuse `wont_work`, require confirmation for `limited`, conditional `applied` marking, create new datasets from the check capture (copy page, plan, and snapshot, insert with viability in `status_detail`, version 1 row, enqueue), route tracked URLs to refresh
    - _Requirements: 3.8, 3.9, 3.11, 4.2, 4.5, 5.1, 5.2, 5.3, 6.4_
  - [ ] 7.2 Handle the unique-constraint race by falling back to refresh
    - _Requirements: 6.7_
  - [ ] 7.3 Add the `POST /ingest/checks/{id}/add` endpoint with per-item outcomes, including `expired`
    - _Requirements: 5.4, 6.5, 6.6_
  - [ ] 7.4 Write integration tests: new → v1; tracked → refresh with no new row; archived → restored; processing → already refreshing; concurrent duplicate → one dataset; double-submitted Add → one dataset; `wont_work` refused; `limited` without confirmation refused
    - _Requirements: 3.8, 3.9, 6.2, 6.4, 6.5, 6.6, 6.7_

- [ ] 8. Implement upload ingestion
  - [ ] 8.1 Implement `POST /uploads` (pre-signed PUT with a content-length limit and rate limiting)
    - _Requirements: 7.1_
  - [ ] 8.2 Implement `upload_parser` reading from S3, with delimiter sniffing, header synonym mapping, size and column checks, usable-row count, and the keep rule; delete the object when invalid
    - Write unit tests including an over-limit file with and without a date column
    - _Requirements: 7.2, 7.3, 7.6_
  - [ ] 8.3 Add `POST /uploads/{id}/preview` and `POST /datasets/upload`, storing the file and `mapping.json` under the dataset and creating the version 1 record
    - Write integration tests
    - _Requirements: 7.2, 7.4_

- [ ] 9. Build the frontend contents of the New Dataset panel
  - [ ] 9.1 Build the URL tab: multi-line input and Check button, per-URL progress, verdict cards with reasons, warnings, evidence, and sample reviews, "Already tracked" notes with link, per-item Retry, selection checkboxes, the 429 message, and `?check=` persistence
    - _Requirements: 1.5, 1.6, 3.7, 3.8, 3.10, 3.12, 6.3_
  - [ ] 9.2 Build Add selected: confirmation for `limited` items, per-item results summary, navigation for single versus multiple adds
    - _Requirements: 3.9, 5.4, 6.5, 6.6_
  - [ ] 9.3 Build the Upload tab: dropzone, direct upload with progress, preview with the over-limit note, column mapper, required name, submit
    - _Requirements: 7.1, 7.2, 7.4, 7.5, 7.6_
  - [ ] 9.4 Write component tests for parsing limits, verdict rendering with samples, disabled `wont_work`, the limited confirmation, the tracked notes, retry, the results summary, and the upload flow
    - _Requirements: 1.2, 3.7, 3.8, 3.9, 6.3, 7.6_

- [ ] 10. Write E2E tests (against the public fixture site)
  - Paste three fixture URLs (one of each verdict), check, add; the `wont_work` one is refused and the others appear in the Library
  - Paste an existing dataset's URL with a tracking parameter; no new row appears and the original goes back to `requested`
  - _Requirements: 3.8, 5.4, 6.4_

- [ ] 11. Write property-based tests for the Correctness Properties
  - Implement one property test per property in the design (Hypothesis), each tagged with its property number
  - _Requirements: 1.3, 3.4, 3.5, 3.6, 3.8, 6.1, 6.2, 6.4, 6.6, 6.7_
