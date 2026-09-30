# Requirements Document

## Introduction

Review Extraction is the shared engine that finds customer reviews on any web page without site-specific code. It is used in two places: `dataset-ingestion` runs it on the first page during a URL Check to predict viability and build an Extraction Plan, and `review-analysis` runs it on every captured page during processing.

**Review detection is AI-first.** An AI **Review Locator** reads a cleaned version of the page and points at the elements that hold each review and its fields. Review text is always read from the page by code, never written by the AI, so the data can't contain invented reviews. Where possible, the Locator's suggested CSS selectors are validated and reused, so later pages are read cheaply and consistently. An evaluation suite measures extraction quality, so the method that works best is the one used.

This spec delivers a library (`app.extraction`) plus its evaluation suite. It has no HTTP endpoints or queues of its own.

Depends on: `platform-foundation` (configuration, AI client wrapper, logging).

## Glossary

- **Extraction Engine**: The `app.extraction` package defined by this spec.
- **Cleaned Page**: A compact text representation of a rendered page's visible content. Scripts, styles, and hidden elements are removed, and each remaining element carries a short reference ID.
- **Review Locator**: An AI call that reads a Cleaned Page and returns the element references of each review and its fields, suggested CSS selectors, the next-page link, the reported total, any blocker, an entity hint, and a confidence level.
- **Verified Review**: A review whose text was read by code from a page element and passed validation.
- **Extraction Plan**: The saved result of locating reviews on a first page: the chosen method (`selectors`, `ai_direct`, or `structured`), validated selectors if any, the next-page rule, and first-page statistics.
- **Structured Review Data**: JSON-LD or microdata `Review` and `AggregateRating` objects embedded in a page.

## Requirements

### Requirement 1: Clean pages for AI reading

**User Story:** As a developer, I want each page reduced to its visible content with stable element references, so that the AI can read it cheaply and point at exact elements.

#### Acceptance Criteria

1. WHEN given rendered HTML, THE Extraction Engine SHALL produce a Cleaned Page that removes scripts, styles, `noscript`, `svg` content, `iframe`, `template`, and hidden elements.
2. THE Cleaned Page SHALL keep visible text, headings, list structure, link targets, and rating-bearing attributes (`aria-label`, `title`, `alt`, `itemprop`, rating data attributes, and class tokens containing `star` or `rating`).
3. THE Cleaned Page SHALL give every kept block element a reference ID and SHALL keep a lookup from each ID to the original element.
4. WHEN a Cleaned Page exceeds the configured token budget (default 30,000), THE Extraction Engine SHALL first drop page boilerplate without review cues (header, footer, navigation, sidebars), and SHALL then split the remaining content into chunks of whole elements with a small overlap.
5. FOR the same HTML, THE Extraction Engine SHALL always produce the same Cleaned Page and reference IDs.

### Requirement 2: Locate reviews with AI

**User Story:** As an analyst, I want the app to find the reviews on any review page by itself, so that I can analyze sites nobody wrote a parser for.

#### Acceptance Criteria

1. WHEN given a Cleaned Page, THE Review Locator SHALL return, through a schema-validated structured response: the reviews found (as element references for the item, text, rating, date, author, and title), excluded non-review items, suggested CSS selectors, the next-page element, the reported total review count, the rating scale, any blocker (`captcha`, `login_wall`, `consent_wall`, `empty`), an entity hint, and a confidence level.
2. THE Extraction Engine SHALL read each review's text, author, date, and title from the referenced elements with code. THE AI SHALL NOT supply review text.
3. THE Extraction Engine SHALL accept an AI-interpreted rating only when the referenced item contains a rating cue and the value is within the page's rating scale.
4. THE Extraction Engine SHALL discard, and count, items whose references don't exist, whose text is shorter than 15 characters, that duplicate another item, or that the Locator marked as Q&A, seller or owner responses, editorial content, or ads.
5. WHEN a page was split into chunks, THE Extraction Engine SHALL run the Locator on the chunks in parallel and merge the results by element reference.
6. THE Review Locator prompt SHALL instruct the model that page content is data and never instructions.
7. IF the Locator's response fails schema validation, THEN THE Extraction Engine SHALL retry once with a repair prompt, and IF that also fails, THEN SHALL raise a retryable "locator unavailable" error.
8. IF the AI provider is unavailable or the global AI limit is reached, THEN THE Extraction Engine SHALL raise a retryable "AI unavailable" error that callers can distinguish from other failures.

### Requirement 3: Parse structured review data

**User Story:** As a developer, I want embedded structured review data read without AI, so that it can cross-check the Locator and serve as a fallback.

#### Acceptance Criteria

1. THE Extraction Engine SHALL read JSON-LD and microdata `Review` objects, including those nested in `Product`, `Organization`, `LocalBusiness`, and `SoftwareApplication` objects, and SHALL read `AggregateRating` review counts.
2. THE Extraction Engine SHALL keep a structured review only if its text also appears in the page's visible text after whitespace and Unicode normalization.

### Requirement 4: Validate selectors and choose a method

**User Story:** As the CTO, I want the cheapest reliable extraction method chosen per site automatically, so that most pages are read without an AI call and none are read badly.

#### Acceptance Criteria

1. WHEN the Locator suggests selectors, THE Extraction Engine SHALL apply them to the original HTML and SHALL treat them as valid only if they select at least a configured share (default 80%) of the Verified Reviews with identical text and select no more than 20% extra elements.
2. THE Extraction Engine SHALL choose `selectors` WHEN the selectors are valid; otherwise `structured` WHEN Structured Review Data holds more verified reviews than the Locator found and their texts agree; otherwise `ai_direct`.
3. THE Extraction Engine SHALL produce an Extraction Plan containing the method, the valid selectors (if any), the rating scale, the next-page rule, the first-page counts (verified, discarded, structured), the reported total, the entity hint, the confidence, the Locator model, and the prompt version.
4. IF the AI is unavailable, THEN THE Extraction Engine SHALL be able to build a plan from Structured Review Data alone, marked as such, so callers can degrade gracefully.

### Requirement 5: Find the next page

**User Story:** As an analyst, I want the app to follow a review list across pages, so that datasets cover more than the first page.

#### Acceptance Criteria

1. THE Extraction Engine SHALL resolve the next page in this order: the plan's next-page rule; then generic patterns (`rel="next"`, links or buttons labeled "Next", "›", or "»", and links to page n + 1 by `?page=`, `&p=`, or `/page/`); then the Locator's next-page element when the page was read by the Locator.
2. WHEN the Locator's next page is a link, THE Extraction Engine SHALL convert it into a reusable rule: a CSS selector, or a URL template when successive page URLs differ only by a page number.
3. THE Extraction Engine SHALL return only candidates on the same registrable domain as the page's final URL.
4. WHEN a page offers only script-driven loading with no URL (infinite scroll or "Load more"), THE Extraction Engine SHALL report that no next page URL exists and SHALL say why.

### Requirement 6: Extract a page with a plan

**User Story:** As a developer, I want one call that extracts a page according to its plan, with automatic fallback, so that processing code stays simple.

#### Acceptance Criteria

1. WHEN asked to extract a page with a plan whose method is `selectors`, THE Extraction Engine SHALL apply the selectors with code. IF the page yields fewer than half the reviews per page seen on the first page and the caller says it is not the last page, THEN THE Extraction Engine SHALL read the page with the Review Locator instead and SHALL report the fallback.
2. WHEN the method is `ai_direct`, THE Extraction Engine SHALL run the Review Locator on the page.
3. WHEN the method is `structured`, THE Extraction Engine SHALL read Structured Review Data, falling back to the Review Locator for a page with none.
4. WHERE Structured Review Data exists on a page, THE Extraction Engine SHALL add structured reviews that the chosen method missed and SHALL report the agreement rate.
5. THE result of extracting a page SHALL include the Verified Reviews, the method actually used, whether a fallback happened, the number discarded, the agreement rate when it applies, and the next-page candidate.

### Requirement 7: Optional host overrides

**User Story:** As a developer, I want the option to add host-specific extraction code later, without making the system depend on it.

#### Acceptance Criteria

1. THE Extraction Engine MAY include host-specific extraction overrides. No feature SHALL require one.
2. WHERE an override exists for a host, THE Extraction Engine SHALL use its output only if it passes the same validation as a Review Locator result.

### Requirement 8: Extraction quality evaluation

**User Story:** As a senior engineer, I want extraction quality measured on real-world page layouts, so that we use whichever method works best and prompt changes can't quietly make it worse.

#### Acceptance Criteria

1. THE repository SHALL contain a labeled evaluation set of at least 15 saved review pages with varied layouts, including at least: pages with Structured Review Data, pages rendered by client-side JavaScript, plain review lists, reviews without ratings, multi-page listings, a page mixing reviews with Q&A or seller responses, and blocker pages. Each page SHALL have hand-checked expected reviews, fields, next page, and viability verdict.
2. THE evaluation SHALL score each method (`selectors`, `ai_direct`, `structured`) and the automatic choice on precision, recall, field accuracy (rating, date, author), next-page detection, AI tokens used, and time taken.
3. WHEN the evaluation runs, THE evaluation SHALL fail if the automatic choice has precision below 0.98 or recall below 0.90 on pages labeled `will_work`.
4. THE evaluation SHALL run on demand and on every change to the page cleaner, the Locator prompt, or the method-choice rules (with the live model), and SHALL write a report.
5. THE default extraction strategy SHALL be the one with the best score in the latest report, and the README SHALL record the scores.
