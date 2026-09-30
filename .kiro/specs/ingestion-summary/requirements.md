# Requirements Document

## Introduction

The Ingestion Summary is the top section of a dataset's detail page. It shows what was ingested and how complete it is, so the analyst can trust that the data is accurate, reasonably complete, and ready for analysis before asking questions. It combines dataset metadata, headline metrics, a page snapshot, completeness indicators, a processing timeline, and a preview of sample reviews. It updates live while the dataset is being processed.

The Guardrailed Q&A chat (`guardrailed-chat`) appears below this summary on the same page.

Depends on: `platform-foundation`, `dataset-ingestion`, `review-analysis`, `dataset-library` (Realtime Channel).

## Glossary

- **Detail Page**: The `/datasets/{id}` route that holds the Ingestion Summary and the chat.
- **Completeness**: How much of the available review data was captured. It is shown as extracted versus reported totals, pages captured, skipped rows, and warnings.

## Requirements

### Requirement 1: Dataset header

**User Story:** As an analyst, I want to see which dataset I'm looking at and where it came from, so that I'm sure I'm analyzing the right source.

#### Acceptance Criteria

1. THE Detail Page SHALL show the dataset name, which the analyst can rename inline.
2. THE Detail Page SHALL show the original URL as the main link. WHEN the final URL differs, it SHALL show "Resolved to: {final URL}" and SHALL show the redirect hops on expand.
3. WHERE the dataset is an upload, the Detail Page SHALL show the file name and optional description in place of URLs.
4. THE Detail Page SHALL show the platform, status badge (using the same display states as the Library), request date, last updated date, and the version being shown with its date ("Data as of Sep 28, 2026 8:20 PM · v3").

### Requirement 2: Headline metrics

**User Story:** As an analyst, I want headline numbers at a glance, so that I understand the dataset's size and overall tone.

#### Acceptance Criteria

1. WHEN the dataset has an active version, the Detail Page SHALL show that version's review count, average rating, overall sentiment breakdown, and review date range, including while a newer version is being processed.
2. WHERE ratings exist, the Detail Page SHALL show a rating distribution bar chart.
3. THE Detail Page SHALL show the identified entity name, category, and description, and SHALL flag them when confidence is low.
4. THE Detail Page SHALL show up to 8 recurring themes with mention counts and sentiment lean.
5. WHERE a metric is not available (for example, no ratings in an upload), the Detail Page SHALL show "Not available" instead of zero.

### Requirement 3: Page snapshot

**User Story:** As an analyst, I want to see a snapshot of the captured page, so that I can confirm visually that the right product page was ingested.

#### Acceptance Criteria

1. WHERE the dataset came from a URL, the Detail Page SHALL show the PNG snapshot for the active version (or for the first version while it is still processing), loaded through a short-lived pre-signed URL.
2. WHEN the analyst clicks the snapshot, the system SHALL show it at full size.
3. IF the snapshot cannot load, THEN the Detail Page SHALL show a placeholder with the reason.

### Requirement 4: Completeness and confidence

**User Story:** As an analyst, I want to know how complete the ingestion was, so that I know how far to trust conclusions drawn from it.

#### Acceptance Criteria

1. WHERE the dataset came from a URL, the Detail Page SHALL show pages captured out of the configured maximum.
2. WHERE the platform reported a total review count, the Detail Page SHALL show "{extracted} of {reported} reviews ingested" as a percentage bar.
3. THE Detail Page SHALL show the extraction method (page selectors found by AI, AI reading each page, structured data, or uploaded file), how many pages were read each way, how many AI-located items were discarded, and the number of skipped rows or reviews.
4. WHEN processing produced warnings, the Detail Page SHALL list them (for example "Stopped at page 4: page failed to load").
5. THE Detail Page SHALL show a readiness indicator for the active version: "Ready for analysis" when it has no warnings and at least 20 reviews; "Ready with caveats" when it has warnings or fewer than 20 reviews; "Not ready" when there is no active version. WHILE a refresh is running or after a refresh failed, the indicator SHALL add a note ("Refreshing — showing v2 until v3 is ready" or "Last refresh failed — showing v2").

### Requirement 5: Sample reviews preview

**User Story:** As an analyst, I want to look through the extracted reviews, so that I can check the extraction quality myself.

#### Acceptance Criteria

1. THE Detail Page SHALL provide a paginated table of the extracted reviews with text (truncated, expandable), rating, date, author, and sentiment.
2. THE review table SHALL support filtering by rating and sentiment, and text search.

### Requirement 6: Processing timeline and live updates

**User Story:** As an analyst, I want to see processing progress live, so that I know what's happening and when the dataset will be ready.

#### Acceptance Criteria

1. THE Detail Page SHALL show a timeline of the `status_detail` events, with timestamps and messages.
2. WHILE status is `requested` or `processing`, the Detail Page SHALL show the latest progress message and a progress state. IF there is no active version yet, THEN the metric panels SHALL show placeholders and the chat input SHALL be disabled. IF there is an active version, THEN its data and the chat SHALL stay available.
3. WHEN a Realtime Channel event arrives for this dataset, the Detail Page SHALL update the status, timeline, and metrics in place, without a page reload. WHEN the active version changes, it SHALL reload the metrics, snapshot, and reviews for the new version.
4. WHEN status becomes `failed`, the Detail Page SHALL show the failure message and a Refresh action to retry. IF an earlier version is still active, THEN it SHALL also say that the analysis is still showing that version.

### Requirement 7: Predicted versus actual result

**User Story:** As an analyst, I want to see how the URL check's prediction compared with what was actually extracted, so that I can judge the source and the team can tune the check.

#### Acceptance Criteria

1. WHERE the dataset came from a URL, the Detail Page SHALL show the Viability Check verdict for the active version, its reasons and warnings, and next to them the actual result: reviews extracted, pages captured, and extraction method.
2. WHEN the actual result differs a lot from the prediction (for example, a `will_work` verdict that yielded fewer than 5 reviews, or a `limited` verdict that yielded over 5× the detected count), the Detail Page SHALL highlight the difference.
