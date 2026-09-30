# Requirements Document

## Introduction

Review Analysis is the background process that runs when a dataset record reaches status `requested`. It collects further review pages up to a configured maximum, extracts the reviews on every page with the Extraction Engine from `review-extraction`, builds a profile of what is being reviewed, and computes summary metrics. When finished, it sets the status to `updated`. Every status change is published so the UI can update live.

The analysis must stay isolated: no external data may enter it. Its outputs are the normalized review corpus and entity profile that `guardrailed-chat` is limited to.

Depends on: `platform-foundation`, `review-extraction`, `dataset-ingestion`.

## Glossary

- **Worker**: The processing-queue consumer (Lambda or container).
- **Extraction Engine**, **Extraction Plan**, **Review Locator**, **Verified Review**: As defined in `review-extraction`.
- **Normalized Review**: `{id, text, rating?, date?, author?, title?, source_page}`.
- **Entity Profile**: What is being reviewed (name, category, short description), derived only from the captured content.
- **MAX_PAGES / MAX_REVIEWS**: Configured crawl limits. Defaults are 10 pages and 1,000 reviews.

## Requirements

### Requirement 1: Start processing

**User Story:** As an analyst, I want processing to start automatically after ingestion, so that I don't have to trigger analysis by hand.

#### Acceptance Criteria

1. WHEN a processing message is received for a dataset with status `requested`, THE Worker SHALL set the status to `processing` and SHALL append a `processing` event to `status_detail`.
2. WHEN a message is a retry of a version already `processing`, THE Worker SHALL continue processing that version. IF the message's version is older than the dataset's current data version, that version is already completed, or the dataset is archived, THEN THE Worker SHALL skip the message without error.
3. THE system SHALL never process the same dataset in two Worker instances at the same time, however many instances are running.
4. WHEN a dataset has stayed in `requested` for more than a configured time (default 5 minutes), THE sweeper SHALL re-enqueue it.
5. WHEN a dataset has stayed in `processing` with no new progress event for more than a configured time (default 20 minutes), THE sweeper SHALL mark that version `failed` with the message "Processing stopped unexpectedly."

### Requirement 2: Collect additional review pages

**User Story:** As an analyst, I want reviews collected from several pages, so that the dataset represents more than the first page.

#### Acceptance Criteria

1. WHEN the Extraction Engine reports a next page for the latest captured page, THE Worker SHALL capture that page to S3 and repeat, until there is no next page, MAX_PAGES is reached, or MAX_REVIEWS is reached.
2. WHEN each page is captured, THE Worker SHALL append a progress event (for example "Captured page 3 of up to 10") to `status_detail`.
3. IF a later page fails to load, THEN THE Worker SHALL stop collecting, SHALL record a warning, and SHALL continue with the pages it already has.
4. THE Worker SHALL wait at least a configured delay (default 1 second) between page requests to the same host.
5. WHEN the dataset is an upload, THE Worker SHALL skip page collection.
6. THE Worker SHALL apply the SSRF protection from `dataset-ingestion` to every page it captures.
7. WHEN the Extraction Engine reports that more reviews load only by script (no next-page URL), THE Worker SHALL record a warning with that reason.

### Requirement 3: Extract reviews

**User Story:** As an analyst, I want every captured page read accurately, so that my analysis rests on real customer feedback.

#### Acceptance Criteria

1. DURING extraction and analysis, THE Worker SHALL use only the dataset's stored objects and the AI provider. It SHALL NOT fetch or include any other data source.
2. THE Worker SHALL extract each captured page with the Extraction Engine using the Extraction Plan saved with the dataset version, and SHALL record per page the method used, reviews found, fallbacks, and discards.
3. THE Worker SHALL remove duplicate reviews by normalized text plus author plus date, including duplicates across pages.
4. WHEN the dataset is an upload, THE Worker SHALL build Normalized Reviews from the column mapping saved with the file, SHALL skip rows with empty text (counting them), and SHALL keep at most MAX_REVIEWS rows using the keep rule shown in the upload preview, recording a warning with the number of rows left out.
5. WHEN extraction completes, THE Worker SHALL save the reviews, the Entity Profile, and the per-page details to `datasets/{id}/reviews/v{n}.json`.

### Requirement 4: Build the entity profile

**User Story:** As an analyst, I want the system to identify what is being reviewed, so that the chat can stay focused on that product or service.

#### Acceptance Criteria

1. THE Worker SHALL derive the entity name, category, and a one-to-two-sentence description using only the captured page content, the plan's entity hint, and the extracted reviews.
2. IF the entity cannot be confidently identified from the content, THEN THE Worker SHALL fall back to the page title (or the upload name) and SHALL mark the profile as low confidence.

### Requirement 5: Compute summary metrics

**User Story:** As an analyst, I want headline numbers for each dataset, so that I can quickly see its size and overall tone.

#### Acceptance Criteria

1. THE Worker SHALL compute the review count, the average rating and rating distribution (when ratings exist), the earliest and latest review dates (when dates exist), pages captured, and rows or reviews skipped.
2. THE Worker SHALL classify each review's sentiment as positive, neutral, or negative, and SHALL compute the overall sentiment breakdown.
3. THE Worker SHALL identify up to 8 recurring themes, each with a label, a mention count, and a positive, neutral, or negative lean.
4. WHERE the page shows a total review count, THE Worker SHALL record it so completeness can be shown as extracted versus reported.
5. THE Worker SHALL store the metrics in the dataset record's `metrics` column.
6. THE metrics SHALL include the extraction method, the number of pages read by selectors and by AI, the number of Review Locator results discarded, and the structured-data agreement rate when it applies.

### Requirement 6: Complete processing

**User Story:** As an analyst, I want to know when a dataset is ready, so that I can start asking questions.

#### Acceptance Criteria

1. WHEN extraction and metrics succeed with at least one review, THE Worker SHALL set the status to `updated`, SHALL set `updated_at`, SHALL set the active version to this version, and SHALL append a completion event with the review count and duration.
2. IF zero reviews are extracted, THEN THE Worker SHALL set the status to `failed` with the message "No reviews found on the captured pages."
3. WHEN processing of a data version ends, THE Worker SHALL complete that version's `dataset_versions` row with `completed_at`, `review_count`, `extraction_method`, and `outcome` (`updated` or `failed`).
4. WHEN processing ends, THE Worker SHALL record the actual result (reviews extracted, pages captured, method used, fallbacks) next to the Viability Check prediction in `status_detail.viability`, so the prediction can be compared with what happened.
5. IF a refresh (data version 2 or later) fails, THEN THE dataset SHALL keep serving the last successful version's reviews and metrics to the summary and chat, and the status event SHALL say so.

### Requirement 7: Handle failures

**User Story:** As an analyst, I want failed processing reported clearly, so that I know to retry or try a different source.

#### Acceptance Criteria

1. IF processing raises an error, THEN THE message SHALL be retried up to 3 times.
2. WHEN the final retry fails, THE Worker SHALL set the status to `failed` and SHALL append an event with an error message that is safe to show users.
3. THE Worker SHALL be idempotent: reprocessing the same dataset version SHALL overwrite that version's outputs without creating duplicates, and SHALL reuse pages already captured for that version.
4. IF the Extraction Engine or another AI step reports that the AI is unavailable, THEN THE Worker SHALL let the message retry with backoff, and WHEN retries are exhausted, THE version SHALL fail with the message "AI service unavailable — try refreshing later."

### Requirement 8: Publish status changes

**User Story:** As an analyst, I want status changes pushed out as they happen, so that open screens update without refreshing.

#### Acceptance Criteria

1. WHEN the status changes or a progress event is appended, THE system SHALL publish a `dataset.status.changed` event with the dataset ID, status, timestamp, message, data version, active version, and metrics (when available).
