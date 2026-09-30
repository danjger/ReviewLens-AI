# Design Document

## Overview

The Library is the React route `/`. It has two sections: the **Add new reviews** panel (contents built in `dataset-ingestion`) and the **Tracked datasets** list. Both are backed by the API service.

Refresh goes through the shared `datasets.refresh_service` from `dataset-ingestion`, so a Refresh button click and a duplicate URL submission behave the same way.

Real-time updates flow from EventBridge (`dataset.status.changed`, `check.updated`, and `chat.exchange.saved`) to the SQS `push-queue`, then to the push consumer (Lambda or container), which posts to the API Gateway WebSocket API and on to browsers. Connection IDs are stored in DynamoDB. The frontend keeps server state in TanStack Query and applies pushed events directly to the cached data.

The app has no sign-in, so everyone sees the same library. Both event types are broadcast to every connection; a browser applies `check.updated` only if it is showing that check.

## Page layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ ReviewLens AI                                                        │
├──────────────────────────────────────────────────────────────────────┤
│ ┌─ ADD NEW REVIEWS ───────────────────────────────────────────────┐  │
│ │ [ URLs ]  [ Upload file ]                                        │  │
│ │ ┌──────────────────────────────────────────────┐                 │  │
│ │ │ Paste up to 10 review page URLs, one per line │  [Check URLs]  │  │
│ │ └──────────────────────────────────────────────┘                 │  │
│ │ ✓ Will work  g2.com/products/acme/reviews  · 25 reviews · pages │  │
│ │ ⚠ Limited    example.com/widget            · 3 reviews · no next│  │
│ │ ✕ Won't work shop.com/x                    · login required     │  │
│ │ ↻ Already tracked as "Acme CRM" — will refresh                  │  │
│ │                                           [Add selected (2)]    │  │
│ └──────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│ TRACKED DATASETS (12)        [search…]  Sort: Last activity ▾  ☐ Archived │
│ ┌──────────────────────────────────────────────────────────────────┐  │
│ │ Acme CRM · g2.com/…   ● Ready   212 reviews  v3  Refreshed 2h ago  ⋯ │
│ │ Widget Pro · …        ◐ Processing — page 4 of 10              ⋯ │
│ └──────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

- The New Dataset panel has a tinted background and a "+" icon heading. The Tracked Datasets section is a plain table. The two sections use different visual treatments so they can't be confused.
- The New Dataset panel collapses to a single "+ Add new reviews" bar once there is at least one dataset and the analyst isn't using it. It expands on click or when a Check is in progress.
- After Add or Refresh, the affected rows get a 3-second highlight and the list scrolls to them.

## Architecture

```mermaid
sequenceDiagram
  participant B as Browser
  participant WS as WS API
  participant C as Connect handler
  participant DDB as DynamoDB ws-connections
  participant EB as EventBridge
  participant XQ as SQS push-queue
  participant P as Push consumer
  B->>WS: connect (no credentials; WS API throttling applies)
  WS->>C: $connect
  C->>DDB: put {connection_id, connected_at, ttl}
  Note over EB: transition(), check handler, and refresh publish events
  EB->>XQ: dataset.status.changed | check.updated | chat.exchange.saved
  XQ->>P: event
  P->>DDB: scan connections (paged)
  P->>WS: postToConnection(event) for each connection
  WS-->>B: {type, ...}
  P->>DDB: delete stale connections (410 Gone)
```

## Components and Interfaces

### API endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/datasets?archived=false&sort=activity&q=` | Returns `[{id, name, main_url, platform, status, display_state, last_message, review_count, data_version, active_version, requested_at, last_refreshed_at, archived_at, refresh_check_id}]` |
| GET | `/datasets/{id}` | Full record (used by `ingestion-summary`), including `active_version` and the `dataset_versions` list |
| PATCH | `/datasets/{id}` | `{name}` rename |
| POST | `/datasets/{id}/refresh` | URL datasets; rate-limited like checks. Creates a Check Session with `origin="refresh"` for the original URL and sends its item to `check-queue`. Returns 202 `{check_id}`. The check handler then starts the refresh automatically, waits for confirmation, or records the failure (dataset-ingestion Requirement 6.9). 409 `ALREADY_REFRESHING` while `requested`, `processing`, or a refresh Check is running |
| POST | `/datasets/{id}/refresh/confirm` | `{check_id}`. Confirms a `limited` refresh verdict; calls `refresh_service.refresh(..., trigger="manual_refresh")` with the Check's capture |
| POST | `/datasets/{id}/refresh/upload` | `{upload_id, mapping}` from the pre-signed upload flow in dataset-ingestion; calls `refresh_service.refresh(..., trigger="upload_replace")` |
| POST | `/datasets/{id}/archive` | Sets `archived_at` |
| POST | `/datasets/{id}/restore` | Clears `archived_at` |

`last_refreshed_at` is `dataset_versions.completed_at` for `active_version`.

`display_state` is derived on the server so the list and detail page agree:

| status | active_version | display_state |
|---|---|---|
| `requested` / `processing` | null | `processing` |
| `requested` / `processing` | set | `ready_refreshing` |
| `updated` | set | `ready` |
| `failed` | set | `ready_refresh_failed` |
| `failed` | null | `failed` |

`refresh_check_id` is set while a refresh Check is running or waiting for confirmation, so the row can show "Checking page…" or "Needs confirmation".

### Realtime components

- `realtime/connect.py` and `realtime/disconnect.py`: tiny `$connect` and `$disconnect` handlers that store and delete `{connection_id, connected_at, ttl=+2h}`. They stay as Lambda functions in both compute modes, because they are glue for the managed WebSocket API rather than application compute. The WebSocket API stage has throttling limits set, since connections need no credentials.
- `handlers.push.PushHandler` (queue `push`, shared consumer runtime): broadcasts each event type to every connection, fanning out `postToConnection` calls concurrently, and deletes connections that return `GoneException`. It is idempotent: a repeated event only re-sends a status the browser already has. Adding push consumer instances raises fan-out throughput.

### Frontend

- `useRealtime()` hook: a single WebSocket per tab, exponential backoff reconnect (1 s → 30 s), and query invalidation on reconnect. It dispatches events to a small event bus.
- `LibraryPage`: contains `NewDatasetPanel` (shell and collapse behavior here, contents from `dataset-ingestion`) and `TrackedDatasetsSection` (header with count, search, sort, "Show archived" toggle, then `DatasetTable`).
- `DatasetTable` rows show a status badge with the live progress message, review count, version, and relative last-refreshed time, plus a row menu with Open, Refresh (disabled while processing), Archive or Restore.
- On a `dataset.status` event: `setQueryData(['datasets'], patchRow)` and `setQueryData(['dataset', id], patch)`. If the ID isn't in the cache (a dataset just created elsewhere), invalidate `['datasets']`.
- On a `check.updated` event: `setQueryData(['check', check_id], patchItem)`.
- The status badge follows `display_state`: Processing (spinner plus the latest message), Ready, Ready · refreshing (spinner), Ready · last refresh failed (warning icon, tooltip with the reason), and Failed (error, tooltip with the last event message). While a refresh Check runs, the row shows "Checking page…"; a `limited` verdict shows a "Needs confirmation" button that opens the reasons.

## Data Models

- DynamoDB `ws-connections`: PK `connection_id`, attribute `connected_at`, TTL on `ttl`.
- Versions: see `dataset_versions` in `dataset-ingestion`. Earlier S3 versions are kept. Readers (summary, reviews, chat) always use `active_version`, the latest version that finished successfully.

## Correctness Properties

Properties are tested with Hypothesis (backend) and fast-check (frontend reducers).

1. **Display state is total and correct.** *For any* combination of status and active version, `display_state` SHALL match the table in this design. _Validates: Requirement 2.5_
2. **Archive never deletes.** *For any* dataset, archiving and then restoring SHALL leave every field except `archived_at` unchanged, and SHALL delete no rows or objects. _Validates: Requirements 5.3, 5.4_
3. **List filtering is exact.** *For any* set of datasets and search text, the list SHALL contain exactly the non-archived datasets whose name or URL contains the text (case-insensitive), in the requested order. _Validates: Requirements 2.1, 2.2, 2.3_
4. **Live updates converge.** *For any* set of status events delivered in any order, applying them with the cache reducer SHALL produce the same row state as applying only the latest event for each dataset. _Validates: Requirements 6.3, 6.4_

## Error Handling

- Refresh while `requested`, `processing`, or with a refresh Check running returns 409 `ALREADY_REFRESHING`, and the UI disables the action anyway.
- A failed refresh re-check (non-200 or `wont_work`) creates no version. The status stays the same, an event `{message: "Refresh failed: 404"}` is appended, and a `dataset.status.changed` event updates the row with the reason.
- A `limited` verdict where the dataset was previously `will_work` leaves the Check item in `awaiting_confirmation`. The row shows "Needs confirmation" until the analyst confirms or the Check expires after 24 hours.
- A push to a stale connection triggers cleanup. A push failure never affects processing.
- If the WebSocket is unavailable, the frontend polls `/datasets` every 10 seconds while any row is `requested` or `processing`, and polls `/ingest/checks/{id}` every 2 seconds while a Check is running.

## Testing Strategy

- **Unit tests:** list query building (filter, sort, archived, `last_refreshed_at`); `display_state` for every row of the table above; archive and restore; refresh endpoints (Check created, 409 guard, confirm, upload replace); push fan-out to every connection; the browser ignoring `check.updated` for a check it isn't showing; stale-connection cleanup; `useRealtime` reconnect and backoff with a mocked WebSocket; cache patch reducers, including an unknown ID invalidating the list.
- **Integration tests:** the API against PostgreSQL and LocalStack for list, archive, restore, and refresh through the Check flow (`will_work` → new version, `dataset_versions` row, enqueue, with no further client call); refresh with the page returning 404 (no version, data kept, event appended); refresh while processing (409); refresh needing confirmation, then confirmed. Publish each event type and assert the right `postToConnection` targets.
- **Frontend component tests:** both sections render with distinct headings and landmarks; no URL input inside the Tracked section; panel collapse and expand rules; empty state points to the panel; highlight after Add or Refresh; row actions; "Show archived" toggle; live row update.
- **E2E:** two browser contexts. Context A has the Library open while B adds a dataset; A sees the new row move through statuses without reloading. Archive, then restore. Refresh a dataset from the row menu and watch it go through processing. Submit an existing URL in the New Dataset panel; the Tracked list highlights the original row rather than showing a new one.
