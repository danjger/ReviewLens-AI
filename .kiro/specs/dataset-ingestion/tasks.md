# Implementation Plan

- [x] 1. Implement URL normalization and validation
  - [x] 1.1 Implement `url_normalizer.normalize()` with a configurable tracking-parameter list
    - Write unit tests for case, `www`, fragment, tracking parameters, query order, and trailing slash
    - _Requirements: 6.1_
  - [x] 1.2 Implement input parsing: up to 10 lines, invalid-line marking, collapsing duplicates within a batch
    - _Requirements: 1.1, 1.2, 1.4_
  - [x] 1.3 Implement `assert_public_host()` for SSRF protection, honoring the test-only allowlist
    - Write unit tests for private, loopback, link-local, and metadata IPs, IPv6, the re-check after redirects, and the allowlist
    - _Requirements: 1.3_
  - [x] 1.4 Implement `probe()` with browser-like headers, manual redirects, a hop limit, loop detection, and hop logging
    - Write integration tests against the fixtures container: 200, 301→200, 302→404, a loop, and more than 10 hops
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6_
  - [x] 1.5 Implement `robots.check()` with a short timeout and a no-restriction fallback
    - _Requirements: 3.12_

- [x] 2. Build the in-process Capture module
  - [x] 2.1 Implement `capture.render()` in the workers image: one reused browser per process with a fresh context per message, SSRF route guard on every browser request, render, one scroll for lazy content, save HTML and a 1280×900 screenshot, read the title, return the main response status and the browser's final URL
    - _Requirements: 1.3, 2.7, 2.8, 3.1, 4.1, 4.3_
  - [x] 2.2 Write integration tests that render fixture pages: normal page, page loading a resource from `127.0.0.1` (blocked), meta-refresh redirect (recorded), 403 main response, lazy-loaded reviews
    - _Requirements: 1.3, 2.7, 2.8, 4.1, 4.3_

- [x] 3. Build the viability assessment
  - [x] 3.1 Implement the rule-based pre-scan for empty shells, challenge pages, and login walls
    - _Requirements: 3.2, 3.5_
  - [x] 3.2 Implement `viability.assess()`: pre-scan, structured data, Review Locator, verification, selector validation, method choice, and the Extraction Plan (calling `extraction.build_plan()` from `review-extraction`)
    - _Requirements: 3.1, 3.2, 3.3, 3.11_
  - [x] 3.3 Implement the verdict rules, plain-language reasons, warnings (robots), samples, and the evidence object, with thresholds from configuration
    - _Requirements: 3.4, 3.5, 3.6, 3.7, 3.12_
  - [x] 3.4 Implement the AI-unavailable fallback (structured data only, verdict at most `limited`)
    - _Requirements: 3.13_
  - [x] 3.5 Write unit tests for the verdict rules from a table of plan outcomes, and for the fallback
    - _Requirements: 3.4, 3.5, 3.6, 3.13_
  - [x] 3.6 Add `assess()` to the extraction evaluation suite as a verdict-accuracy column with the 0.90 threshold
    - _Requirements: 3.14_

- [x] 4. Build Check Sessions and the check handler
  - [x] 4.1 Create the DynamoDB `check-sessions` item layout (items as a map for per-item conditional updates) and the `check-queue` consumer wiring
    - _Requirements: 4.4_
  - [x] 4.2 Implement `handlers.check.CheckHandler`: conditional claim, probe, capture to the check prefix, client-redirect handling, assess, duplicate lookup, write the item and plan, publish `check.updated`; enforce `CHECK_TIMEOUT_S`; mark `error` on the final attempt
    - _Requirements: 2.2, 2.4, 2.7, 2.8, 3.1, 3.10, 6.2, 6.3_
  - [x] 4.3 Add the `POST /ingest/checks`, `GET /ingest/checks/{id}`, and per-item retry endpoints with per-IP and global rate limiting
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.6, 3.10_
  - [x] 4.4 Write integration tests with the AI stub: each fixture ends in the expected item state, verdict, plan, and event; a crashed handler leaves the item in `error` and retry works; the same message delivered twice produces one result; 429 after the limit
    - _Requirements: 1.6, 3.1, 3.10_

- [x] 5. Add the data model changes
  - Write a migration adding `normalized_url` (unique partial index for URL datasets), `normalized_final_url` (indexed), and the `dataset_versions` table with `extraction_method`
  - _Requirements: 5.1, 6.2, 6.7_

- [x] 6. Build the Refresh Service (shared with dataset-library)
  - [x] 6.1 Implement `refresh_service.refresh()` with a required capture: guard for already refreshing, restore when archived, a transactional version increment, copy the capture and plan, insert a `dataset_versions` row with the trigger, add the `refresh_requested` event, enqueue
    - _Requirements: 6.4, 6.5, 6.6, 6.8_
  - [x] 6.2 Implement refresh-origin handling in the check handler: automatic refresh, wait for confirmation, or failed-refresh event with no version row
    - _Requirements: 6.9_
  - [x] 6.3 Write unit and integration tests for each branch, including two concurrent refresh calls producing one version
    - _Requirements: 6.4, 6.5, 6.6, 6.8, 6.9_

- [x] 7. Implement Add
  - [x] 7.1 Implement `add_items()`: refuse `wont_work`, require confirmation for `limited`, conditional `applied` marking, create new datasets from the check capture (copy page, plan, and snapshot, insert with viability in `status_detail`, version 1 row, enqueue), route tracked URLs to refresh
    - _Requirements: 3.8, 3.9, 3.11, 4.2, 4.5, 5.1, 5.2, 5.3, 6.4_
  - [x] 7.2 Handle the unique-constraint race by falling back to refresh
    - _Requirements: 6.7_
  - [x] 7.3 Add the `POST /ingest/checks/{id}/add` endpoint with per-item outcomes, including `expired`
    - _Requirements: 5.4, 6.5, 6.6_
  - [x] 7.4 Write integration tests: new → v1; tracked → refresh with no new row; archived → restored; processing → already refreshing; concurrent duplicate → one dataset; double-submitted Add → one dataset; `wont_work` refused; `limited` without confirmation refused
    - _Requirements: 3.8, 3.9, 6.2, 6.4, 6.5, 6.6, 6.7_

- [x] 8. Implement upload ingestion
  - [x] 8.1 Implement `POST /uploads` (pre-signed PUT with a content-length limit and rate limiting)
    - _Requirements: 7.1_
  - [x] 8.2 Implement `upload_parser` reading from S3, with delimiter sniffing, header synonym mapping, size and column checks, usable-row count, and the keep rule; delete the object when invalid
    - Write unit tests including an over-limit file with and without a date column
    - _Requirements: 7.2, 7.3, 7.6_
  - [x] 8.3 Add `POST /uploads/{id}/preview` and `POST /datasets/upload`, storing the file and `mapping.json` under the dataset and creating the version 1 record
    - Write integration tests
    - _Requirements: 7.2, 7.4_

- [x] 9. Build the frontend contents of the New Dataset panel
  - [x] 9.1 Build the URL tab: multi-line input and Check button, per-URL progress, verdict cards with reasons, warnings, evidence, and sample reviews, "Already tracked" notes with link, per-item Retry, selection checkboxes, the 429 message, and `?check=` persistence
    - _Requirements: 1.5, 1.6, 3.7, 3.8, 3.10, 3.12, 6.3_
  - [x] 9.2 Build Add selected: confirmation for `limited` items, per-item results summary, navigation for single versus multiple adds
    - _Requirements: 3.9, 5.4, 6.5, 6.6_
  - [x] 9.3 Build the Upload tab: dropzone, direct upload with progress, preview with the over-limit note, column mapper, required name, submit
    - _Requirements: 7.1, 7.2, 7.4, 7.5, 7.6_
  - [x] 9.4 Write component tests for parsing limits, verdict rendering with samples, disabled `wont_work`, the limited confirmation, the tracked notes, retry, the results summary, and the upload flow
    - _Requirements: 1.2, 3.7, 3.8, 3.9, 6.3, 7.6_

- [x] 10. Write E2E tests (against the public fixture site)
  - Paste three fixture URLs (one of each verdict), check, add; the `wont_work` one is refused and the others appear in the Library
  - Paste an existing dataset's URL with a tracking parameter; no new row appears and the original goes back to `requested`
  - _Requirements: 3.8, 5.4, 6.4_

- [x] 11. Write property-based tests for the Correctness Properties
  - Implement one property test per property in the design (Hypothesis), each tagged with its property number
  - _Requirements: 1.3, 3.4, 3.5, 3.6, 3.8, 6.1, 6.2, 6.4, 6.6, 6.7_
- [x] 12. Fix the upload keep-rule bug and two integration-test defects (see design.md → Known Issues)
  - [x] 12.1 Upload keep rule must honour the CONFIRMED mapping (real product bug, Requirement 7.6)
    - `app/ingestion/service.py::create_from_upload` writes the analyst's confirmed `mapping` into `mapping.json` but takes `keep_rule`/`will_keep` from `upload_parser.preview_upload(...)`, which derives them from the parser's AUTO-SUGGESTED mapping and ignores the confirmed one. When the analyst maps a date column the synonym table doesn't auto-detect (e.g. a `when` header), `keep_rule` wrongly falls to `first_in_file` even though a date column IS mapped. Verified: `suggest_mapping(["review","when"]) → {'text':'review'}`.
    - Fix: derive the keep rule from the CONFIRMED mapping — `most_recent_by_date` when `"date"` is in the confirmed mapping, else `first_in_file` — and build `mapping.json` and the `requested` event consistently from the confirmed mapping (keep `will_keep = min(usable_rows, MAX_REVIEWS)`). Prefer exposing a small keep-rule helper in `upload_parser` that takes an explicit mapping, so the rule lives in one place. Behaviour fix only; do not change the ORM/raw-SQL inserts.
    - Make `tests/integration/ingestion/test_upload_service_int.py::test_over_limit_upload_records_keep_rule` pass, and add/adjust a unit test covering a confirmed-but-not-auto-detected date column.
    - _Requirements: 7.6_
  - [x] 12.2 Fix the `will_work` verdict integration test (test defect, not product)
    - `test_check_handler_full_int.py::TestPlainListFullAiPath::test_will_work_with_selectors_plan_and_event` expects `will_work`, but the `plain_list` fixture has only 4 reviews and `viability_min_reviews` is 5, so `limited` is CORRECT. Preferred fix (preserves the test's intent to exercise the `will_work` selectors path): add a 5th review to the `plain_list` fixture and update the scripted Locator (`_plain_list_locator_response` / `_PLAIN_LIST_REVIEW_SNIPPETS`) to point at 5 real rendered refs and bump `reported_total` to 5. If adding a fixture review is awkward or changes other tests that count on 4, instead set `VIABILITY_MIN_REVIEWS=4` in this test's env — flag which you chose and why. Do NOT weaken the product `will_work` rule.
    - _Requirements: 3.4_
  - [x] 12.3 Clear the origin secret in the add/check HTTP integration tests (test hygiene)
    - `test_add_service_int.py::test_add_endpoint_new_and_wont_work_mix`, `::test_add_endpoint_expired_session_returns_expired`, and `test_check_handler_full_int.py::TestCheckEndpointRateLimit::test_429_after_limit` fail with 403 because they call the API via `TestClient` without the `X-Origin-Verify` header while `ORIGIN_VERIFY_SECRET` is set. Clear `ORIGIN_VERIFY_SECRET` in those suites' fixtures (as the library/summary integration suites already do) so the origin guard runs in local-dev bypass. Proven: with the secret cleared all three pass. (The rate-limit test must still reach its 429 assertion, not 403.)
    - _Requirements: 2.4_
  - Verify with the worker CONSUMERS STOPPED and a fresh DB (do not run `make test-int` with `--wait`, which restarts consumers): run `tests/integration/ingestion/test_upload_service_int.py`, `tests/integration/ingestion/test_add_service_int.py`, and `tests/integration/handlers/test_check_handler_full_int.py` to green, plus `make lint` and the unit/property suite (expect 1443). Do NOT touch the two concurrency failures (Known Issues C) — they are undiagnosed and out of scope here.
- [x] 13. Fix the refresh concurrency guard (Correctness Property 7) — see design.md → Known Issues C
  - Real product bug (verified by deep dive): `app/datasets/refresh_service.py` splits the claim across two transactions — `_claim_new_version` bumps `data_version` under `SELECT ... FOR UPDATE` + `WHERE status NOT IN ('requested','processing')` but does NOT change `status`, commits, and releases the lock; the status transition to `requested` happens later in a SEPARATE `db.status.transition` call. So the guard never fires for a concurrent racer: two refreshes both see `status='updated'`, both pass the guard, and `data_version` goes 1→2→3. Violates Property 7 / Requirement 6.6.
  - Fix: make the claim atomically mark the dataset in-flight. In the SAME guarded, row-locked UPDATE in `_claim_new_version`, also set `status = 'requested'` (alongside the existing `data_version` bump, `archived_at = NULL`, and `status_detail - 'refresh_check_id'`), so a second racer's `status NOT IN ('requested','processing')` guard correctly matches no row and the caller returns `already_refreshing`.
  - Preserve the event/publish contract: every status change still goes through `db.status` (Requirement 4.4) and exactly ONE `dataset.status.changed` event must be published per new version. Since the claim now sets `status='requested'` itself, reconcile the subsequent `transition(..., REQUESTED, "refresh_requested", ...)` so it does not double-write or emit a second/duplicate event — e.g. have the claim append the `refresh_requested` event + publish once (move the event into the claim transaction), or keep `transition` as the single event emitter but make it tolerate the status already being `requested`. Choose the approach that keeps "one event per version" and keeps the version bump + version-row insert + status mark in one transaction. Do not change the queue enqueue or `_copy_capture` ordering (copy still happens after the claim, before enqueue).
  - Keep restore-on-refresh (`archived_at = NULL` → `restored_and_refreshed`) working, and the `refresh_check_id` key-drop. While editing these raw statements, add `CAST(:id AS uuid)` to the `:id` binds in `_claim_new_version` (same uuid-cast class as task 15) so they stay correct — flag if any bind is left uncast.
  - Verify with the worker CONSUMERS STOPPED and a fresh DB (do NOT run `make test-int` with `--wait`): `tests/integration/datasets/test_refresh_service_int.py` (incl. `test_two_concurrent_refreshes_produce_exactly_one_version` and `test_many_concurrent_refreshes_produce_exactly_one_version`) and `tests/integration/ingestion/test_add_service_int.py::test_concurrent_duplicate_new_url_yields_one_dataset` must pass; the rest of those files stay green; `make lint` passes; and the unit/property suite stays green (expect 1446 — update `tests/unit/datasets/test_refresh_service.py` only if the claim's control flow changed in a way its fakes assert on, without weakening what it checks). Confirm exactly one `dataset.status.changed` event per new version (the refresh integration test already asserts a single enqueue; also sanity-check the event count if feasible).
  - _Requirements: 6.6, 6.8, 4.4_

- [x] 14. Match separator variants in upload column detection (`review_text`) — see design.md → Known Issues
  - Root cause (live CSV upload): header `review_text` was rejected ("No review text column") — matching was exact-equality on a lower/trimmed header, and the synonym was `"review text"` (space), so the underscore form matched nothing.
  - Fix: `_normalize_header` now collapses `_`/`-`/repeated spaces to a single space, so `review_text`/`review-text`/`Review  Text` match. Generalizes to `star_rating`, `review_date`, etc. Added 6 regression cases.
  - Verify: parser unit tests green (44); live preview auto-detects text/rating/date/author on the sample. DONE (code + local proof); live re-verify after deploy.
  - _Requirements: 7.2_
