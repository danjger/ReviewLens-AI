# Implementation Plan

- [ ] 1. Build the list API
  - Implement `GET /datasets` with archived filter, activity sort, text search, and derived `main_url`, `last_message`, `last_refreshed_at` (from `dataset_versions`), `display_state`, and `refresh_check_id`
  - Implement `GET /datasets/{id}` (with `active_version` and the versions list) and `PATCH /datasets/{id}` (rename)
  - Write unit tests for every `display_state` combination and integration tests for filter, sort, and search combinations
  - _Requirements: 2.1, 2.2, 2.3, 2.5, 3.1_

- [ ] 2. Build the archive and restore API
  - Implement `POST /datasets/{id}/archive` and `/restore` so they only change `archived_at`
  - Include `archived_at` in the detail response so the chat can be made read-only (implemented in `guardrailed-chat`)
  - Write integration tests showing no DB rows or S3 objects are deleted, and that list visibility changes
  - _Requirements: 5.1, 5.3, 5.4, 5.5_

- [ ] 3. Build the refresh API
  - [ ] 3.1 Implement `POST /datasets/{id}/refresh`: create a refresh-origin Check Session for the original URL, send its item to `check-queue`, return 202 `{check_id}`; 409 while requested, processing, or a refresh Check is running
    - The check handler's refresh-origin handling is built in `dataset-ingestion` task 6.2
    - _Requirements: 4.1, 4.2, 4.4, 4.7_
  - [ ] 3.2 Implement `POST /datasets/{id}/refresh/confirm` for `limited` verdicts on datasets previously `will_work`
    - _Requirements: 4.3_
  - [ ] 3.3 Implement `POST /datasets/{id}/refresh/upload` using the pre-signed upload flow and `trigger="upload_replace"`
    - _Requirements: 4.5_
  - [ ] 3.4 Write integration tests: `will_work` refresh creates a version with no further client call; previous versions kept; 404 page → no version and data kept; 409 while processing; confirmation flow; the manual path and the duplicate-submission path produce the same kind of record
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6, 4.7_

- [ ] 4. Build the real-time backend
  - [ ] 4.1 Add the CDK WebSocket API with stage throttling, the connect and disconnect handlers, and the EventBridge rule to `push-queue`
    - _Requirements: 6.1_
  - [ ] 4.2 Build `handlers.push.PushHandler` on the shared consumer runtime: broadcast both event types with concurrent fan-out, clean up stale connections
    - _Requirements: 6.2, 6.5_
  - [ ] 4.3 Write unit tests for fan-out and stale cleanup, and an integration test from an EventBridge event through `push-queue` to `postToConnection` calls, with the push consumer running as a container
    - _Requirements: 6.1, 6.2, 6.5_

- [ ] 5. Build the real-time frontend
  - Implement the `useRealtime` hook with a single socket, backoff reconnect, and refetch after reconnect
  - Add polling fallbacks for the list (10 s) and for active Checks (2 s)
  - Write cache patch reducers for the list, detail, and check queries; invalidate the list for an unknown dataset ID
  - Write unit tests with a mocked WebSocket
  - _Requirements: 6.1, 6.3, 6.4, 6.5, 6.6_

- [ ] 6. Build the Library page
  - [ ] 6.1 Build the page layout with two labeled sections ("Add new reviews" and "Tracked datasets ({count})") and distinct visual treatments; stack on narrow screens
    - _Requirements: 1.1, 1.5_
  - [ ] 6.2 Build the `NewDatasetPanel` shell: URL and Upload tabs hosting the `dataset-ingestion` components; collapse and expand rules; expanded when the library is empty
    - _Requirements: 1.2, 1.5, 1.6_
  - [ ] 6.3 Build `TrackedDatasetsSection`: search, sort, "Show archived" toggle, and `DatasetTable` with a `display_state` badge (including "Ready · refreshing" and "Ready · last refresh failed"), live progress, review count, version, last refreshed, and row navigation
    - _Requirements: 1.3, 2.1, 2.2, 2.3, 2.5, 3.1, 3.2, 5.2, 6.3_
  - [ ] 6.4 Build the row actions: Open, Refresh (disabled while processing or checking; shows "Checking page…", the failure reason, or a "Needs confirmation" dialog with the verdict reasons), Archive with confirmation, Restore
    - _Requirements: 4.3, 4.4, 5.1, 5.3_
  - [ ] 6.5 Highlight and scroll to affected rows after Add or Refresh
    - _Requirements: 1.4_
  - [ ] 6.6 Add empty, loading, and error-with-retry states
    - _Requirements: 1.6, 2.4_
  - [ ] 6.7 Write component tests: distinct sections, no URL input in the Tracked section, collapse rules, empty state, highlight, all actions, a live row update
    - _Requirements: 1.1, 1.3, 1.4, 1.6, 2.4, 6.3_

- [ ] 7. Write E2E tests
  - Two contexts: a live row moves through statuses without a reload
  - Archive, then Show archived, then Restore
  - Refresh from the row menu and watch the row go through processing
  - Submit an already-tracked URL (including an archived one) in the New Dataset panel; the original row is highlighted, restored if needed, and no new row appears
  - _Requirements: 1.4, 4.1, 5.1, 5.2, 5.3, 5.6, 6.2, 6.3_

- [ ] 8. Write property-based tests for the Correctness Properties
  - Implement one property test per property in the design (Hypothesis for backend, fast-check for frontend), each tagged with its property number
  - _Requirements: 2.1, 2.2, 2.3, 2.5, 5.3, 5.4, 6.3, 6.4_
