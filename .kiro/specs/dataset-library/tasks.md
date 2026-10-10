# Implementation Plan

- [x] 1. Build the list API
  - Implement `GET /datasets` with archived filter, activity sort, text search, and derived `main_url`, `last_message`, `last_refreshed_at` (from `dataset_versions`), `display_state`, and `refresh_check_id`
  - Implement `GET /datasets/{id}` (with `active_version` and the versions list) and `PATCH /datasets/{id}` (rename)
  - Write unit tests for every `display_state` combination and integration tests for filter, sort, and search combinations
  - _Requirements: 2.1, 2.2, 2.3, 2.5, 3.1_

- [x] 2. Build the archive and restore API
  - Implement `POST /datasets/{id}/archive` and `/restore` so they only change `archived_at`
  - Include `archived_at` in the detail response so the chat can be made read-only (implemented in `guardrailed-chat`)
  - Write integration tests showing no DB rows or S3 objects are deleted, and that list visibility changes
  - _Requirements: 5.1, 5.3, 5.4, 5.5_

- [x] 3. Build the refresh API
  - [x] 3.1 Implement `POST /datasets/{id}/refresh`: create a refresh-origin Check Session for the original URL, send its item to `check-queue`, return 202 `{check_id}`; 409 while requested, processing, or a refresh Check is running
    - The check handler's refresh-origin handling is built in `dataset-ingestion` task 6.2
    - _Requirements: 4.1, 4.2, 4.4, 4.7_
  - [x] 3.2 Implement `POST /datasets/{id}/refresh/confirm` for `limited` verdicts on datasets previously `will_work`
    - _Requirements: 4.3_
  - [x] 3.3 Implement `POST /datasets/{id}/refresh/upload` using the pre-signed upload flow and `trigger="upload_replace"`
    - _Requirements: 4.5_
  - [x] 3.4 Write integration tests: `will_work` refresh creates a version with no further client call; previous versions kept; 404 page → no version and data kept; 409 while processing; confirmation flow; the manual path and the duplicate-submission path produce the same kind of record
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6, 4.7_

- [x] 4. Build the real-time backend
  - [x] 4.1 Add the CDK WebSocket API with stage throttling, the connect and disconnect handlers, and the EventBridge rule to `push-queue`
    - _Requirements: 6.1_
  - [x] 4.2 Build `handlers.push.PushHandler` on the shared consumer runtime: broadcast both event types with concurrent fan-out, clean up stale connections
    - _Requirements: 6.2, 6.5_
  - [x] 4.3 Write unit tests for fan-out and stale cleanup, and an integration test from an EventBridge event through `push-queue` to `postToConnection` calls, with the push consumer running as a container
    - _Requirements: 6.1, 6.2, 6.5_

- [x] 5. Build the real-time frontend
  - Implement the `useRealtime` hook with a single socket, backoff reconnect, and refetch after reconnect
  - Add polling fallbacks for the list (10 s) and for active Checks (2 s)
  - Write cache patch reducers for the list, detail, and check queries; invalidate the list for an unknown dataset ID
  - Write unit tests with a mocked WebSocket
  - _Requirements: 6.1, 6.3, 6.4, 6.5, 6.6_

- [x] 6. Build the Library page
  - [x] 6.1 Build the page layout with two labeled sections ("Add new reviews" and "Tracked datasets ({count})") and distinct visual treatments; stack on narrow screens
    - _Requirements: 1.1, 1.5_
  - [x] 6.2 Build the `NewDatasetPanel` shell: URL and Upload tabs hosting the `dataset-ingestion` components; collapse and expand rules; expanded when the library is empty
    - _Requirements: 1.2, 1.5, 1.6_
  - [x] 6.3 Build `TrackedDatasetsSection`: search, sort, "Show archived" toggle, and `DatasetTable` with a `display_state` badge (including "Ready · refreshing" and "Ready · last refresh failed"), live progress, review count, version, last refreshed, and row navigation
    - _Requirements: 1.3, 2.1, 2.2, 2.3, 2.5, 3.1, 3.2, 5.2, 6.3_
  - [x] 6.4 Build the row actions: Open, Refresh (disabled while processing or checking; shows "Checking page…", the failure reason, or a "Needs confirmation" dialog with the verdict reasons), Archive with confirmation, Restore
    - _Requirements: 4.3, 4.4, 5.1, 5.3_
  - [x] 6.5 Highlight and scroll to affected rows after Add or Refresh
    - _Requirements: 1.4_
  - [x] 6.6 Add empty, loading, and error-with-retry states
    - _Requirements: 1.6, 2.4_
  - [x] 6.7 Write component tests: distinct sections, no URL input in the Tracked section, collapse rules, empty state, highlight, all actions, a live row update
    - _Requirements: 1.1, 1.3, 1.4, 1.6, 2.4, 6.3_

- [x] 7. Write E2E tests
  - Two contexts: a live row moves through statuses without a reload
  - Archive, then Show archived, then Restore
  - Refresh from the row menu and watch the row go through processing
  - Submit an already-tracked URL (including an archived one) in the New Dataset panel; the original row is highlighted, restored if needed, and no new row appears
  - _Requirements: 1.4, 4.1, 5.1, 5.2, 5.3, 5.6, 6.2, 6.3_

- [x] 8. Write property-based tests for the Correctness Properties
  - Implement one property test per property in the design (Hypothesis for backend, fast-check for frontend), each tagged with its property number
  - _Requirements: 2.1, 2.2, 2.3, 2.5, 5.3, 5.4, 6.3, 6.4_
- [x] 9. Expose `status_detail` and the upload description on the detail record
  - Add `status_detail` (the append-only event log plus the `redirects` and `viability` blocks) and `description` (upload datasets) to `DatasetDetail.to_dict()` in `app/datasets/library.py`, so `GET /datasets/{id}` returns the fields the detail page consumes: ProcessingTimeline (`events`), the DatasetHeader "Resolved to" redirect hops (`redirects`), and the PredictionPanel (`viability`), plus the upload file description. The list response (`GET /datasets`) is unchanged.
  - Confirm whether the identified-entity profile should be surfaced on the detail `metrics` (review-analysis writes it into `reviews/v{n}.json`, not the `metrics` dict). If it should, include it here; otherwise record that EntityCard reads it from the reviews file and leave `metrics` as is.
  - Update the design's "API endpoints" row for `GET /datasets/{id}` to list the added fields so the design and the implementation agree.
  - Extend the existing detail unit and integration tests to assert the new fields are present (seed `status_detail` with events, redirects, and a viability block; and an upload with a description). Keep the `GET /datasets` list-response tests unchanged.
  - _Requirements: 3.1, 6.4_

- [x] 10. Add a rendered-page thumbnail to each tracked-dataset row
  - 10.1 Backend: expose a snapshot thumbnail URL on the list row
    - The permanent snapshot already exists at `storage.keys.dataset_snapshot(id, version)` = `datasets/{id}/snapshot/v{n}.png`, written by `app/ingestion/service.py` (new dataset) and `app/datasets/refresh_service.py` (refresh) — but ONLY when a screenshot was captured. Upload datasets have no snapshot (Requirement 7.5), and a URL whose capture produced none also won't; the field MUST be nullable and the UI must degrade gracefully.
    - Add a `thumbnail_url` (nullable) to `DatasetSummary.to_dict()` in `app/datasets/library.py`, pointing at the dataset's ACTIVE version snapshot (`active_version`; fall back to the latest completed version). Serve it as a time-limited presigned S3 GET via `app/storage/s3.py` (add a `presigned_get` helper if absent), or a CloudFront path if the Edge stack already fronts the bucket — pick one and note why. Return `null` when `active_version` is unset or the snapshot object does not exist (`s3.object_exists`). The detail record (`GET /datasets/{id}`) may expose the same.
    - Keep it cheap for a list of many rows: do NOT HEAD every object on every list call if that is slow — prefer deriving the key from `active_version` and letting the browser 404 to the placeholder, or batch-check. Record the choice.
    - Upload datasets have no screenshot, so `thumbnail_url` stays null for them and the UI shows a CSV icon (task 10.2). IF the optional CSV text-preview is pursued, the small top-of-file sample should come from an existing field or a short, bounded read of `raw/v{n}/upload.csv` (`storage.keys.dataset_raw_upload`) — bounded to the first few rows; do not stream whole files into the list response. Treat this as optional/nice-to-have, not required for the icon fallback.
    - Tests: unit test that a dataset with an active snapshot yields a non-null URL and one without yields null (upload dataset, and a URL dataset pre-first-success); keep existing list-response tests green.
    - _Requirements: 2.1, 3.1_
  - 10.2 Frontend: render the thumbnail on each dataset card
    - In the tracked-dataset list card, show the `thumbnail_url` as a small rendered-page thumbnail (fixed aspect box, lazy-loaded, `object-fit: cover`). Use a `data-testid` and never depend on counts/dates in E2E (testing steering).
    - Fallback is source-type aware (not a generic blank):
      - URL dataset with no snapshot yet (e.g. still processing, or capture produced none): neutral "rendering…/no preview" placeholder, never a broken image.
      - UPLOAD (CSV) dataset: these never have a page screenshot (Requirement 7.5), so show a CSV/file icon by default. Optionally, instead of the icon, render a lightweight preview of the TOP of the file (first few rows/header) as the thumbnail — a small, clipped text/table card generated client-side from a short sample. Keep it cheap and clipped; the icon is the safe default and the text-preview is a nice-to-have.
    - Drive the choice off the row's `source_type` (`url` vs `upload`) already on the summary, plus whether `thumbnail_url` is present — do NOT infer upload-ness from a null URL.
    - Accessible: meaningful `alt` (e.g. "Rendered page for {name}"), not decorative-only; keep it keyboard/screen-reader sane.
    - Tests: component test for the image-present and placeholder-fallback cases (MSW-mocked list); update the list E2E to assert the thumbnail testid is present.
    - _Requirements: 2.1_
  - Cross-spec note: the snapshot is PRODUCED in review-extraction/capture + copied by dataset-ingestion/review-analysis; this task only READS the existing artifact and surfaces it. If the active-version snapshot turns out not to be written in some path, STOP and flag the producing spec rather than writing snapshots here.

- [x] 11. Visual design pass: a polished, original style (not an Apple/Amazon clone)
  - Goal: a pleasant, well-engineered look inspired by the clarity of apple.com and the information density of amazon.com, WITHOUT imitating either (no copied layouts, marks, type pairings, or signature colors). Original palette, type scale, and spacing.
  - [x] 11.1 Establish a lightweight design system in the frontend: design tokens (color, spacing, radius, shadow, typography scale) as CSS variables / a theme module; a small set of primitives (Button, Card, Badge/StatusPill, Input, Section header) used across routes. One component per file (structure steering), `PascalCase` components.
    - Pick a distinctive-but-restrained palette and a readable type scale; document the tokens and the rationale briefly in the frontend README or a `design-system.md`.
  - [x] 11.2 Apply the system to the main surfaces: the New Dataset panel, the tracked-dataset list (incl. the task-10 thumbnail), the dataset detail/summary page, and the chat view — consistent spacing, elevation, and status colors. Keep the New-URL entry visually separate from the tracked list (Requirement 1.1/1.6 — existing rule).
  - [x] 11.3 Accessibility is a hard requirement, not polish: WCAG AA contrast on the chosen palette, visible focus states, hit targets, reduced-motion support. Note that full WCAG validation needs manual AT testing + expert review; automate what we can (axe in component tests).
  - Tests: keep existing component/axe tests green; add axe checks on the restyled primitives. No behavioral change — this is presentation only, so API and data flow are untouched.
  - Judgment/needs-a-person: the specific palette/typography is a taste decision; propose 1–2 directions (tokens + a sample screen) for sign-off before applying site-wide, rather than restyling everything first.
  - PROGRESS (this session): added `frontend/src/styles/theme.css` (CSS-variable tokens: color/type/spacing/radius/shadow, dark-mode + reduced-motion) and `base.css` (reset, typography, and styling for the library page, New-Dataset panel, tracked table, dataset-row + task-10 thumbnail, status badge, buttons/cards), imported in `main.tsx`. The frontend had NO css before this. Also fixes a real a11y bug: `.visually-hidden` had no rule, so screen-reader-only text was visible. TWO palette DIRECTIONS ship for sign-off: A "Slate & Teal" (default) and B "Ink & Amber" (`<html data-theme="b">`). NEXT (needs sign-off): pick A or B, then apply the system to the remaining detail/summary + chat surfaces (11.2 complete) and add axe checks on the primitives (11.3).
  - UPDATE: palette A "Slate & Teal" signed off and finalized (removed the temporary ThemeSwitcher + Direction B + the data-theme logic). Also added two UI items requested from the live app: a "Back to datasets" link on the detail page, and a "Clear" action on a checked-URL verdict card to dismiss a failed/non-viable result (wont_work/invalid/duplicate/error) from the list (view-only). Styled both in base.css. Remaining 11.2: fuller polish of the chat panel internals; 11.3 axe on primitives.
  - UPDATE (polish): styled the chat panel internals (history/exchange bubbles, chat input, citation chip, decline tag) and refined the detail two-column grid + slot headings. Added axe checks for the new primitives (DatasetRow URL+CSV rows, dismissable VerdictCard; DatasetThumbnail already had them). Full frontend suite: 493 passed (the 6 failing are pre-existing local-only chat-streaming env failures that pass in CI). Palette A is the sole theme.
  - _Requirements: 1.1, 1.6_
