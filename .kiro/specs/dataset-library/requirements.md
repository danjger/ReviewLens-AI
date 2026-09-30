# Requirements Document

## Introduction

The Dataset Library is the Portal's home screen. It has two clearly separate areas:

- **New Dataset**: where the analyst submits new URLs (or a file), sees whether each URL will work, and adds them. Its contents are specified in `dataset-ingestion`.
- **Tracked Datasets**: every URL or file submitted before, with its status. From here the analyst can open, refresh, archive, or restore a dataset.

This spec also owns the real-time push channel. Status changes from ingestion checks, analysis, and refreshes reach every connected browser, so the list and any open detail page update without a reload.

Depends on: `platform-foundation`, `dataset-ingestion` (New Dataset panel contents, Refresh Service), `review-analysis` (processing and status events).

## Glossary

- **Library**: The home screen, containing the New Dataset panel and the Tracked Datasets list.
- **New Dataset panel**: The area for submitting and checking new URLs or uploading a file.
- **Tracked Datasets list**: The list of previously submitted datasets.
- **Realtime Channel**: The authenticated WebSocket connection that delivers `dataset.status.changed` and `check.updated` events to browsers.
- **Refresh**: Re-capturing and re-processing an existing dataset as a new data version, through the Refresh Service defined in `dataset-ingestion`.

## Requirements

### Requirement 1: Separate new submissions from tracked datasets

**User Story:** As an analyst, I want a clear distinction between where I request a new URL and the URLs I've already submitted, so that I don't confuse the two.

#### Acceptance Criteria

1. THE Library SHALL show the New Dataset panel and the Tracked Datasets list as visually separate, labeled sections: "Add new reviews" and "Tracked datasets ({count})".
2. THE New Dataset panel SHALL be the only place on the Library where new URLs or files can be entered.
3. THE Tracked Datasets list SHALL NOT contain URL inputs. Its actions SHALL only act on existing datasets (Open, Refresh, Archive, Restore).
4. WHEN an Add or Refresh completes, the affected dataset SHALL be highlighted briefly in the Tracked Datasets list, so the analyst can see where it went.
5. WHERE the viewport is narrow, the two sections SHALL stack, with the New Dataset panel collapsible and the Tracked Datasets list below it.
6. WHEN there are no tracked datasets, the New Dataset panel SHALL be expanded and the Tracked Datasets section SHALL show an empty state that points to it.

### Requirement 2: List tracked datasets

**User Story:** As an analyst, I want to see all my datasets and their status in one place, so that I can pick one to work on.

#### Acceptance Criteria

1. THE Tracked Datasets list SHALL show every dataset that is not archived. Each row SHALL show the name, main URL (the original URL or the upload file name), platform, status, review count (when available), data version, first submitted date, and last refreshed date.
2. THE list SHALL sort by last activity, newest first, and SHALL let the analyst sort by name, status, or last refreshed date.
3. THE list SHALL provide a text filter that matches name or URL.
4. WHILE the list is loading, it SHALL show a loading placeholder. IF loading fails, THEN it SHALL show an error with a retry action.
5. WHERE a dataset has usable data from an earlier version, its status SHALL show that the data is still usable: "Ready · refreshing" while a refresh is `requested` or `processing`, and "Ready · last refresh failed" after a failed refresh. A plain "Failed" status SHALL be shown only when the dataset has no usable version.

### Requirement 3: Open a dataset

**User Story:** As an analyst, I want to open a dataset, so that I can see its summary and ask questions.

#### Acceptance Criteria

1. WHEN the analyst selects a dataset row, the system SHALL go to that dataset's detail page at a stable URL (`/datasets/{id}`).
2. THE detail page URL SHALL be bookmarkable and shareable, and SHALL load directly for anyone who opens it.

### Requirement 4: Refresh a dataset

**User Story:** As an analyst, I want to refresh a dataset from its source, so that my analysis reflects new reviews.

#### Acceptance Criteria

1. WHEN the analyst chooses Refresh on a URL dataset whose status is `updated` or `failed`, the system SHALL start a Check of the original URL (reachability and viability, as in `dataset-ingestion`) and SHALL show its progress on the row. WHEN the verdict allows it, the system SHALL capture a new data version, set the status to `requested`, and enqueue processing, even if the analyst has left the page.
2. IF the re-check returns anything other than HTTP 200, or a `wont_work` verdict, THEN the system SHALL keep the existing data and status, SHALL append a failed-refresh event with the reason, and SHALL show the reason on the row.
3. IF the re-check returns `limited` where the dataset was previously `will_work`, THEN the system SHALL show the reasons and ask the analyst to confirm before continuing.
4. WHILE a dataset is `requested` or `processing`, or a refresh Check for it is running, the Refresh action SHALL be disabled.
5. WHEN the analyst chooses Refresh on an upload dataset, the system SHALL ask for a replacement file and SHALL process it as a new data version.
6. WHEN a refresh completes successfully, the previous data version's files SHALL be kept in S3, and the summary and chat SHALL switch to the new version. IF it fails, THEN they SHALL keep using the previous version.
7. Refreshes started from the New Dataset panel (a duplicate URL) and from the Refresh action SHALL use the same Refresh Service and produce the same record of the refresh.

### Requirement 5: Archive and restore

**User Story:** As an analyst, I want to archive datasets I'm finished with, so that the list stays focused without losing data.

#### Acceptance Criteria

1. WHEN the analyst chooses Archive and confirms, the system SHALL set `archived_at` and SHALL remove the dataset from the default list.
2. THE Tracked Datasets section SHALL provide a "Show archived" toggle that lists archived datasets with a Restore action.
3. WHEN the analyst chooses Restore, the system SHALL clear `archived_at` and SHALL return the dataset to the default list.
4. Archiving SHALL NOT delete any database rows or S3 objects.
5. WHILE a dataset is archived, its chat SHALL be read-only (history is visible, but no new questions can be asked).
6. WHEN an archived dataset's URL is submitted again in the New Dataset panel, the dataset SHALL be restored and refreshed, as specified in `dataset-ingestion`.

### Requirement 6: Real-time updates

**User Story:** As an analyst, I want statuses and check results to appear live, so that I know when a dataset is ready without refreshing the page.

#### Acceptance Criteria

1. WHEN an authenticated user opens the Portal, the frontend SHALL open a Realtime Channel connection.
2. WHEN a `dataset.status.changed` event is published, the system SHALL deliver it to every connected client within 2 seconds under normal conditions.
3. WHEN a Library client receives a status event, it SHALL update that dataset's row (status, progress message, review count, version, last refreshed) in place.
4. WHEN a detail-page client for the same dataset receives a status event, it SHALL update the status, summary data, and Q&A refresh markers in place.
5. WHEN a `check.updated` event is published, the system SHALL deliver it to connected browsers, and a New Dataset panel showing that Check SHALL update that URL's verdict card. Browsers not showing that Check SHALL ignore it.
6. IF the Realtime Channel disconnects, THEN the frontend SHALL reconnect with backoff and SHALL refetch current state after reconnecting.
