# Requirements Document

## Introduction

The Ingestion Module lets an analyst add review datasets in one of three ways. The first is to submit one or more URLs for products or entities on public review platforms. The second is to upload a CSV or other tabular file of reviews. The third is to upload the saved HTML of a review page the analyst opened in their own browser, for sites the app's own capture cannot read; the Extraction Engine then reads reviews from that uploaded page exactly as it reads them from a live capture (see Requirement 8).

URL submission has two steps: **Check**, then **Add**.

- **Check:** For each URL, the module confirms it is reachable, follows redirects, and rejects anything that does not end in HTTP 200. It then renders the page and **analyzes the returned HTML, using AI to find where the reviews are**, to predict whether review extraction will work. Each URL gets a clear verdict with reasons: *Will work*, *Limited*, or *Won't work*. The Check also produces an **Extraction Plan** that `review-analysis` reuses.
- **Add:** The analyst adds the URLs that can work. A URL that is already tracked is never added twice. Instead, the original dataset is refreshed.

Adding a new URL captures its content and snapshot to S3 and creates a dataset record with status `requested`. That record hands the dataset to `review-analysis`.

Depends on: `platform-foundation`, `review-extraction` (the Extraction Engine). Shares the refresh service with `dataset-library`.

## Glossary

- **Target URL**: A URL the analyst submits.
- **Final URL**: The URL that returned HTTP 200 after all redirects were followed.
- **Normalized URL**: The Target URL or Final URL in canonical form, used for duplicate matching (see Requirement 6).
- **Viability Check**: Analysis of the rendered HTML that predicts whether reviews can be extracted.
- **Verdict**: The Viability Check result: `will_work`, `limited`, or `wont_work`, with reasons.
- **Review Locator**: The AI-driven page reader defined in `review-extraction`. Given a cleaned page, it finds the reviews, their fields, the next-page link, and any blocker, and proposes reusable CSS selectors.
- **Extraction Plan**: The Review Locator's output after verification: the chosen extraction method, validated selectors (if any), the next-page rule, and the verified reviews found on the first page.
- **Check Session**: A short-lived record of one Check run. It holds each URL's verdict, Extraction Plan, and captured content until the URLs are added or the session expires. Anyone with the Check's ID can view it; the app has no user accounts.
- **Refresh**: Re-capturing and re-processing an existing dataset as a new data version (shared with `dataset-library`).
- **HTML Upload**: An ingestion path where the analyst saves a review page's rendered HTML in their own browser and uploads that file. The Extraction Engine runs on the uploaded markup exactly as it runs on a URL capture; the analyst's browser performed the rendering the app cannot (see Requirement 8).
- **Upload Capture**: A capture produced from an uploaded file rather than a live render, used by the shared Refresh Service in place of a Check capture. For an HTML Upload it carries the uploaded HTML and its Extraction Plan; for a tabular upload it carries the file and column mapping.
- **Source URL**: The original page URL an analyst may optionally supply with an HTML Upload. It is stored for display, provenance, and duplicate matching only, and is never fetched by the system.

## Requirements

### Requirement 1: Submit URLs

**User Story:** As an analyst, I want to paste one or more review page URLs, so that I can start tracking several products at once.

#### Acceptance Criteria

1. THE New Dataset panel SHALL accept up to 10 URLs per submission, one per line.
2. WHEN a line is not a well-formed `http` or `https` URL, the system SHALL mark that line invalid with a message, and SHALL still check the other lines.
3. THE system SHALL refuse URLs that resolve to private, loopback, or link-local IP addresses (SSRF protection), and SHALL give those URLs a `wont_work` verdict. This protection SHALL apply to every request made while checking or capturing, including redirects and every request the headless browser makes for the page.
4. WHEN the same URL appears more than once in one submission, after normalization, the system SHALL check it once and SHALL show the repeated lines as duplicates.
5. WHILE a Check is running, the UI SHALL show progress for each URL and SHALL block a second submission.
6. URL checks SHALL be rate-limited per client IP and globally, as defined in `platform-foundation`. WHEN a limit is hit, the panel SHALL say when checks can resume.

### Requirement 2: Validate reachability and follow redirects

**User Story:** As an analyst, I want unreachable URLs rejected immediately, so that the library never fills with broken datasets.

#### Acceptance Criteria

1. WHEN a Target URL is checked, the system SHALL follow all HTTP redirects, up to a configured maximum (default 10).
2. IF the final response status is anything other than 200, THEN the system SHALL give the URL a `wont_work` verdict with the status (for example "Not found (404)"), and SHALL NOT allow it to be added.
3. IF the redirect limit is exceeded, or a redirect loop is detected, THEN the system SHALL give the URL a `wont_work` verdict with an explanation.
4. WHEN the final response is 200, the system SHALL use the Final URL for capture and analysis, and SHALL keep the Target URL as the main URL shown to the user.
5. WHEN redirects occurred, the system SHALL record each hop's URL, status code, and timestamp, and SHALL carry them into `status_detail.redirects` when the URL is added.
6. THE reachability probe SHALL send browser-like request headers, so that sites which block non-browser clients are judged the same way the headless browser will see them.
7. THE status of the main page response in the headless browser SHALL also be 200. IF it is not, THEN the URL SHALL get a `wont_work` verdict with that status.
8. WHEN the page redirects itself in the browser (a meta refresh or script redirect), the system SHALL record that hop as a client-side redirect, and SHALL use the page the browser ends on as the Final URL.

### Requirement 3: Predict viability from the returned HTML

**User Story:** As an analyst, I want to know before adding a URL whether the app can actually read reviews from it, so that I don't wait for processing only to find it failed.

#### Acceptance Criteria

1. WHEN a URL returns 200, the system SHALL render it in a headless browser and SHALL run the Extraction Engine from `review-extraction` on the rendered HTML to find the reviews dynamically, without depending on site-specific code.
2. THE Viability Check SHALL determine, at least:
   - which reviews are on the page, with their text and, where present, rating, date, author, and title;
   - whether the same reviews can be read with reusable selectors (so later pages can be read without another AI call);
   - any structured review data on the page (JSON-LD or microdata), used as a cross-check;
   - the next-page link or other sign of more review pages;
   - the total review count the page reports, if any;
   - blockers: a bot or CAPTCHA challenge, a login or paywall, a consent wall, or an empty JavaScript shell with no readable content.
3. EVERY review the Review Locator returns SHALL be verified against the page text. Reviews whose text does not appear on the page SHALL be discarded and SHALL NOT count toward the verdict.
4. THE system SHALL assign `will_work` WHEN at least a configured number (default 5) of reviews are verified, no blocker is found, the Locator's confidence is not low, and either a next page was found or the page doesn't report many more reviews than it shows (reported total no more than twice the verified count).
5. THE system SHALL assign `wont_work` WHEN a blocker is found or no reviews are verified.
6. THE system SHALL assign `limited` in every other case, for example: fewer than the minimum verified reviews; low Locator confidence; more than 20% of the Locator's reviews failing verification; or many more reviews reported than shown with no next page found.
7. EACH Verdict SHALL show plain-language reasons and the evidence found: verified reviews on the page, the extraction method that will be used (selectors, AI reading each page, or structured data), whether more pages were found, the reported total (if shown), and any blocker. It SHALL also show two or three sample reviews so the analyst can see what was found.
8. THE system SHALL NOT allow a `wont_work` URL to be added.
9. WHEN the analyst adds a `limited` URL, the system SHALL ask for confirmation and SHALL show the reasons again.
10. THE Viability Check SHALL finish within a configured time per URL (default 60 seconds, to allow for the headless browser's cold start and the AI call). IF it does not, THEN the URL SHALL get a `wont_work` verdict with reason "Page took too long to load," and the analyst SHALL be able to retry that URL.
11. WHEN a URL is added, its Verdict and evidence SHALL be saved in `status_detail.viability`, and its Extraction Plan SHALL be saved with the capture, so `review-analysis` reuses it and the prediction can later be compared with the actual result.
12. WHERE the site's `robots.txt` disallows the page for general crawlers, the Verdict SHALL include that as a warning. This warning SHALL NOT change the verdict by itself.
13. IF the AI provider is unavailable or the global AI limit is reached, THEN the Check SHALL fall back to structured data only, SHALL mark the verdict at most `limited` with the reason "AI page reading unavailable," and SHALL let the analyst retry later.
14. THE verdict SHALL be scored against the labeled pages in the extraction evaluation suite (`review-extraction`), and the evaluation SHALL fail if verdict accuracy is below 0.90.

### Requirement 4: Capture page content and snapshot

**User Story:** As an analyst, I want the page content and a picture of the page saved, so that analysis runs on a fixed copy and I can confirm the right page was captured.

#### Acceptance Criteria

1. WHEN a URL is checked, the system SHALL store the rendered HTML and a PNG screenshot of the top of the page (viewport 1280×900) in a temporary Check Session area in S3.
2. WHEN the URL is added, the system SHALL move the HTML, screenshot, and Extraction Plan to the dataset's permanent location under its dataset ID.
3. THE system SHALL extract the page `<title>` and store it as `page_title`.
4. WHEN a Check Session expires (24 hours) without being added, its temporary objects SHALL be deleted automatically.
5. IF moving objects or creating the record fails, THEN the system SHALL delete any partial permanent objects and SHALL NOT create the dataset record.

### Requirement 5: Create the dataset record

**User Story:** As a developer, I want each new dataset recorded with full metadata, so that downstream processing and the UI have one source of truth.

#### Acceptance Criteria

1. WHEN a new (not duplicate) viable URL is added, the system SHALL insert a dataset record with a unique ID, page title, request date, last update date (set to the request date), original URL, final URL, normalized URLs, platform, data version 1, no active version yet, and status `requested`, together with a version 1 record in `dataset_versions` with trigger `initial`.
2. WHEN the record is inserted, `status_detail` SHALL contain a `requested` event with a timestamp, the redirect details, and the Viability Check result.
3. WHEN the record is committed, the system SHALL enqueue a processing message that carries the dataset ID and data version.
4. WHEN a single URL is added, the UI SHALL take the analyst to its detail page. WHEN several URLs are added, the UI SHALL stay on the Library, where the new rows appear with their live status.

### Requirement 6: One dataset per URL (refresh instead of duplicate)

**User Story:** As an analyst, I want a URL that is already tracked to refresh the existing dataset rather than create a copy, so that each product has one history.

#### Acceptance Criteria

1. THE system SHALL normalize URLs before matching: lowercase the scheme and host, remove a leading `www.`, remove the fragment, remove known tracking parameters (`utm_*`, `gclid`, `fbclid`, `ref`, and a configurable list), sort the remaining query parameters, and remove a trailing slash.
2. THE system SHALL treat a URL as already tracked WHEN its normalized Target URL or normalized Final URL matches the normalized original or final URL of any existing dataset, including archived datasets.
3. WHEN the Check finds that a URL is already tracked, its Verdict SHALL show "Already tracked as '{dataset name}' — adding will refresh it," with a link to that dataset. IF the verdict for a tracked URL is `wont_work`, THEN the card SHALL instead say that the page can't be read right now and the existing data is unchanged.
4. WHEN an already-tracked URL is added, the system SHALL NOT create a new dataset. It SHALL refresh the original dataset, using the capture from this Check as the new data version.
5. WHERE the original dataset is archived, adding the URL SHALL restore it and refresh it, and SHALL tell the analyst it was restored.
6. WHILE the original dataset is `requested` or `processing`, adding the URL SHALL NOT start another refresh. It SHALL tell the analyst that the dataset is already being refreshed.
7. THE Data Store SHALL enforce uniqueness of the normalized original URL for URL datasets, so that concurrent submissions cannot create duplicates.
8. WHEN a refresh is started this way, the system SHALL append a `refresh_requested` event to `status_detail` that records the submitted URL and the trigger `duplicate_submission`.
9. WHEN a Check is started by the Refresh action in the Library for an existing URL dataset, the system SHALL check the dataset's original URL the same way, and:
   - IF the verdict is `will_work`, or `limited` for a dataset whose previous verdict was also `limited`, THEN it SHALL start the refresh automatically, without the analyst needing to stay on the page;
   - IF the verdict is `limited` for a dataset previously `will_work`, THEN it SHALL wait for the analyst to confirm;
   - IF the verdict is `wont_work`, THEN it SHALL NOT create a new data version, SHALL keep the existing data and status, and SHALL append a failed-refresh event with the reason.

### Requirement 7: Upload tabular review data

**User Story:** As an analyst, I want to upload a CSV of reviews, so that I can analyze data from platforms that can't be scraped or that I exported myself.

#### Acceptance Criteria

1. THE system SHALL accept `.csv` and `.tsv` files up to the configured size limit (default 10 MB). The browser SHALL upload the file directly to storage, so the file size is not limited by the API's request size.
2. WHEN a file is uploaded, the system SHALL require a review text column and SHALL accept optional rating, date, author, and title columns. It SHALL detect columns from common header names and let the analyst correct the mapping before submitting.
3. IF the file has no usable review text column, cannot be parsed, or exceeds the size limit, THEN the system SHALL reject it with a specific message, SHALL NOT create a dataset, and SHALL delete the uploaded file.
4. WHEN a valid upload is submitted, the system SHALL store the file and the confirmed column mapping under the new dataset, SHALL create a dataset record with `source_type = upload`, a user-supplied name (required), optional source description, data version 1 with a version 1 record, and status `requested`, and SHALL enqueue processing.
5. WHEN an upload is shown in the UI, the system SHALL display the file name in place of a URL and SHALL skip the page snapshot.
6. WHEN the file has more usable rows than the configured maximum reviews per dataset, the preview SHALL say so before submitting, and SHALL say which rows will be kept: the most recent by date when a date column is mapped, otherwise the first rows in the file.

### Requirement 8: Upload saved page HTML as a capture

**User Story:** As an analyst, I want to upload the saved HTML of a review page I opened in my own browser, so that I can analyze sites that block the app's own capture (datacenter-IP blocking, client-side review widgets, or consent walls) while still having review text read from the page itself rather than typed into a spreadsheet.

#### Acceptance Criteria

1. THE system SHALL accept `.html`, `.htm`, and `.mhtml` single-file saved pages up to the configured upload size limit (the `MAX_UPLOAD_MB` limit shared with Requirement 7.1), and the browser SHALL upload the file directly to storage through the same pre-signed PUT mechanism used for tabular uploads (Requirement 7.1), so the file size is not limited by the API's request size.
2. IF an uploaded file is not an accepted HTML type, exceeds the size limit, or cannot be read as text, THEN the system SHALL reject it with a specific message, SHALL NOT create a dataset, and SHALL delete the uploaded file (mirroring Requirement 7.3).
3. WHEN an HTML upload is submitted for assessment, THE system SHALL run the Extraction Engine (`app.extraction.build_plan()` from `review-extraction`) on the uploaded HTML to produce an Extraction Plan and a Verdict, applying the same viability rules, thresholds, and method choice used by the URL Check (Requirements 3.2 through 3.7).
4. EVERY review the Review Locator returns from an uploaded HTML file SHALL be verified against the uploaded page's text, and reviews whose text does not appear on the page SHALL be discarded and SHALL NOT count toward the verdict (the same verification as Requirement 3.3). THE Review Locator SHALL only point at elements in the uploaded page; it SHALL NOT supply or generate review text.
5. WHILE assessing an uploaded HTML file, THE system SHALL read only the static saved markup and SHALL NOT perform any live network fetch of the uploaded page's links, scripts, images, iframes, or other sub-resources.
6. WHERE a screenshot is rendered from an uploaded HTML file, THE rendering SHALL pass every sub-resource request through `assert_public_host` and SHALL block requests to private, loopback, link-local, or metadata addresses (the same capture route guard as Requirement 1.3).
7. WHEN the assessment of an uploaded HTML file completes, and before the analyst adds the dataset, THE system SHALL show a preview analogous to the URL verdict card: the Verdict with plain-language reasons and evidence, the extraction method that will be used, and two or three verified sample reviews copied from the uploaded page.
8. WHEN a valid HTML upload is added, THE system SHALL create a dataset record with a `source_type` that marks it an HTML upload (distinct from the tabular-upload `source_type = upload`), a required analyst-supplied name, an optional source description, and an optional original page URL supplied by the analyst that SHALL be stored for display and provenance only and SHALL NOT be fetched.
9. WHEN a valid HTML upload is added, THE system SHALL set data version 1 with a version 1 record in `dataset_versions` (trigger `initial`), status `requested`, SHALL save the Verdict and evidence in `status_detail.viability`, SHALL store the uploaded HTML and its Extraction Plan as the dataset's version 1 capture, and SHALL enqueue a processing message carrying the dataset ID and data version (mirroring Requirements 4.2, 5.1, 5.2, and 5.3).
10. WHEN an HTML upload is shown in the UI, THE system SHALL display the uploaded file name in place of a URL and SHALL skip the live page snapshot, and WHERE a screenshot was rendered from the uploaded HTML the UI MAY show it.
11. WHERE the analyst does not supply an original page URL, THE HTML upload SHALL NOT participate in the normalized-URL duplicate matching of Requirement 6.
12. WHERE the analyst supplies an original page URL whose normalized form matches an existing tracked dataset (Requirement 6.2), THE system SHALL refresh that dataset using the uploaded HTML as the new data version through the shared Refresh Service, rather than creating a duplicate, aligning with Requirements 6.3 through 6.6.
13. IF the AI provider is unavailable or the global AI limit is reached while assessing an uploaded HTML file, THEN the system SHALL fall back to structured data only, SHALL mark the verdict at most `limited` with the reason "AI page reading unavailable," and SHALL let the analyst retry later (mirroring Requirement 3.13).
14. THE New Dataset panel SHALL offer HTML upload alongside the URL and CSV paths, with a dropzone that uploads through the pre-signed mechanism with a progress indicator, then runs the assessment, then shows the Verdict, verified sample reviews, a required name field, and optional source URL and description fields before the analyst submits. THE verdict badge SHALL convey its state with text, not color alone (matching the accessibility pattern in Requirement 3.7 and the URL verdict card).

### Requirement 9: A large server-rendered review fixture for evaluation and tests

**User Story:** As a developer, I want at least one large server-rendered review fixture, so that the URL-check and HTML-upload paths can reach a genuine `will_work` verdict in automated tests instead of only ever reaching `limited`.

#### Acceptance Criteria

1. THE public fixtures site (`/fixtures-site`) and the extraction evaluation suite (`review-extraction`) SHALL include at least one server-rendered review page fixture that contains at least 20 reviews, each with review text and, where applicable, rating, date, and author.
2. WHEN the Extraction Engine runs on the large server-rendered fixture, THE assessment SHALL produce a `will_work` verdict under the default viability thresholds (at least the default minimum of 5 verified reviews, no blocker, and Locator confidence not low), as defined in Requirement 3.4.
3. THE large server-rendered fixture SHALL be loadable over HTTP by the URL-check E2E tests and SHALL be usable as an uploaded HTML file by the HTML-upload tests, so both paths exercise a `will_work` outcome from the same source page.
