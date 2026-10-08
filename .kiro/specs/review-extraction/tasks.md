# Implementation Plan

- [x] 1. Set up the extraction package
  - Create `backend/app/extraction/` with the public API stubs, the `AIUnavailable` and `LocatorUnavailable` error types, and the data models from the design
  - Add `selectolax`, `extruct`, `tldextract` (bundled suffix list), `dateparser`, and `hypothesis` to the backend dependencies
  - _Requirements: 2.7, 2.8_

- [x] 2. Implement the page cleaner
  - [x] 2.1 Implement removal rules, kept attributes, reference IDs, and the ref-to-element lookup
    - _Requirements: 1.1, 1.2, 1.3, 1.5_
  - [x] 2.2 Implement token counting, boilerplate trimming, and chunking with overlap
    - _Requirements: 1.4_
  - [x] 2.3 Write unit tests for each rule and property tests for Properties 2, 3, 4, and 5
    - _Requirements: 1.1, 1.3, 1.4, 1.5_

- [x] 3. Implement structured-data parsing
  - Read JSON-LD and microdata reviews at any depth under the listed containers, verify against visible text, read `AggregateRating` counts
  - Write unit tests with JSON-LD and microdata fixtures, including unverifiable reviews
  - _Requirements: 3.1, 3.2_

- [x] 4. Implement the Review Locator
  - [x] 4.1 Write `prompts/locator_v1.md` and implement the Claude call with the forced tool schema, chunk fan-out and merge, one repair retry, and the two error types
    - _Requirements: 2.1, 2.5, 2.6, 2.7, 2.8_
  - [x] 4.2 Implement post-processing: read fields from refs, date parsing, rating checks, discards by reason
    - _Requirements: 2.2, 2.3, 2.4_
  - [x] 4.3 Write unit tests with recorded Locator responses (including one that supplies its own text), a schema contract test, and property tests for Properties 1 and 6
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.7_

- [x] 5. Implement selector validation and the Extraction Plan
  - [x] 5.1 Implement selector validation (agreement and over-selection)
    - _Requirements: 4.1_
  - [x] 5.2 Implement `build_plan()`: method choice, next-page rule derivation, plan fields, and the structured-only degraded plan
    - _Requirements: 4.2, 4.3, 4.4_
  - [x] 5.3 Write unit tests for the method-choice table and a property test for Property 8
    - _Requirements: 4.1, 4.2_

- [x] 6. Implement pagination
  - Implement `next_page()` with the plan rule, generic patterns, the Locator's next-page element, URL-template inference, the same-domain filter, and the "no URL" reason
  - Write unit tests for each rule and a property test for Property 7
  - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [x] 7. Implement per-page extraction
  - Implement `extract_page()` with dispatch by method, the selector-yield fallback, the structured fallback, the structured cross-check and agreement rate, and the full `PageResult`
  - Write unit tests for every branch
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

- [x] 8. Add the optional host-override registry
  - Implement the empty registry and route override output through the same validation
  - Write a unit test with a sample override that passes and one that fails validation
  - _Requirements: 7.1, 7.2_

- [ ] 9. Build the extraction evaluation suite
  - [x] 9.1 Create `/evals/extraction/` with the page layout, `labels.yaml` format, and `run.py` scorer that writes `report.md`; seed it with synthetic pages covering every layout type
    - _Requirements: 8.1, 8.2_
  - [x] 9.2 Add the CI job (`evals.yml`) that runs on changes to the cleaner, Locator prompt, or method-choice rules and on demand, failing below the thresholds
    - _Requirements: 8.3, 8.4_
  - [~] 9.3 Add saved copies of real review pages to reach at least 15 labeled pages, and publish all pages to `/fixtures-site` (**needs a person**: choose the sites and hand-check the labels)
    - _Requirements: 8.1_
  - [~] 9.4 Run the evaluation with the live model, tune the cleaner and `locator_v1.md` until the thresholds pass, set the default `EXTRACTION_STRATEGY` from the report, and record the scores in the README (**needs** `ANTHROPIC_API_KEY`)
    - _Requirements: 8.3, 8.5_
