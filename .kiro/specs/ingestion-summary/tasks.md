# Implementation Plan

- [x] 1. Build the summary API endpoints
  - [x] 1.1 Implement `GET /datasets/{id}/snapshot-url` with a 5-minute pre-signed URL and a 404 for upload datasets
    - Write unit tests
    - _Requirements: 3.1_
  - [x] 1.2 Implement `GET /datasets/{id}/reviews` with pagination, rating and sentiment filters, text search, and an in-memory cache keyed by version
    - Write unit tests for filter combinations and the page boundary
    - Return a `DATA_MISSING` error when the `active_version` file is absent
    - _Requirements: 5.1, 5.2_
  - [x] 1.3 Write integration tests for the detail, reviews, and snapshot endpoints with seeded fixtures
    - _Requirements: 2.1, 3.1, 5.1_

- [x] 2. Build the detail page shell and header
  - [x] 2.1 Create the `/datasets/{id}` route with the responsive layout and a not-found state
    - _Requirements: 1.4_
  - [x] 2.2 Build `DatasetHeader`: inline rename, original URL, "Resolved to" line with expandable redirect hops, upload file name and description, `display_state` badge, dates, and "Data as of … · v{n}"
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 3. Build the metrics components
  - [x] 3.1 Build `MetricsPanel` with "Not available" handling
    - _Requirements: 2.1, 2.5_
  - [x] 3.2 Build `RatingDistributionChart` with an accessible table alternative
    - _Requirements: 2.2_
  - [x] 3.3 Build `EntityCard` with the low-confidence flag, and `ThemesList`
    - _Requirements: 2.3, 2.4_

- [x] 4. Build the snapshot and completeness components
  - [x] 4.1 Build `SnapshotCard` with the lightbox, the failure placeholder, and a refetch when the URL expires
    - _Requirements: 3.1, 3.2, 3.3_
  - [x] 4.2 Build `CompletenessPanel`: pages captured, extracted versus reported bar, extraction method, skipped count, warnings
    - _Requirements: 4.1, 4.2, 4.3, 4.4_
  - [x] 4.3 Build `ReadinessBanner` with the three threshold rules for the active version, plus the refreshing and refresh-failed notes
    - _Requirements: 4.5_
  - [x] 4.4 Build `PredictionPanel`: verdict, reasons, and warnings next to the actual result, with the large-difference highlight
    - _Requirements: 7.1, 7.2_

- [x] 5. Build the reviews table
  - Server-paginated table with expandable text and filters for rating, sentiment, and debounced search
  - _Requirements: 5.1, 5.2_

- [x] 6. Add the timeline and live updates
  - [x] 6.1 Build `ProcessingTimeline` from `status_detail.events`
    - _Requirements: 6.1_
  - [x] 6.2 Wire `useRealtime` to patch the detail cache, and refetch the detail, reviews, and snapshot when `active_version` changes
    - _Requirements: 6.3_
  - [x] 6.3 Add the first-version processing state (skeletons, latest message, chat input disabled), the refresh-in-progress state (data and chat stay available), and the failed state (message, Refresh action, and "still showing v{n}" when an earlier version is active)
    - _Requirements: 6.2, 6.4_

- [x] 7. Write component tests
  - Cover each component with complete data, missing data, warnings, upload versus URL, each readiness threshold and note, the prediction panel, a live `active_version` change, and a refresh in progress keeping data available
  - Run axe accessibility checks
  - _Requirements: 1.3, 2.5, 4.4, 4.5, 6.2, 6.3, 7.2_

- [x] 8. Write the E2E test
  - Open a processed fixture dataset and verify the header, metrics, snapshot, completeness, and filtered reviews
  - Add a dataset and watch the detail page move from processing to ready without a reload
  - _Requirements: 2.1, 3.1, 4.2, 5.2, 6.3_

- [x] 9. Write property-based tests for the Correctness Properties
  - Implement one property test per property in the design (Hypothesis for backend, fast-check for frontend), each tagged with its property number
  - _Requirements: 2.5, 4.5, 5.1, 5.2, 7.2_
