# Implementation Plan

- [x] 1. Write the prompts and load the Corpus
  - [x] 1.1 Write `prompts/system_v1.md` with the scope, evidence, and security clauses and the decline template; add a prompt loader with version tracking
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 4.3_
  - [x] 1.2 Implement Corpus loading from `reviews/v{active_version}.json`, with a warm in-memory cache keyed by `(id, version)` and the `CHAT_CORPUS_TOKEN_BUDGET` truncation note
    - _Requirements: 2.1_
  - [x] 1.3 Implement message assembly: system prompt, cached `<reviews>` block, the last 6 Exchanges with the same `conversation_id` from the current data version only, wrapped question, optional pre-check hint
    - Write unit tests for cache-control placement, history truncation, exclusion of older-version and other conversations' Exchanges, and inclusion of the hint
    - _Requirements: 2.6, 4.1, 7.3, 9.6_

- [x] 2. Implement the scope pre-check
  - Write `prompts/precheck_v1.md`; implement the call with a JSON schema, a 1.5-second timeout, and parallel execution
  - Write unit tests for the timeout fallback and label parsing
  - _Requirements: 3.5_

- [x] 3. Implement post-processing
  - [x] 3.1 Extract citations, remove IDs that are not in the Corpus, and attach a snippet (text, rating, date) for each remaining citation
    - _Requirements: 2.2, 2.5_
  - [x] 3.2 Build the prompt-leak detector with the decline replacement
    - _Requirements: 4.3_
  - [x] 3.3 Implement decline detection and scope tagging: marker check, hidden `<scope>` tag stripping, pre-check merge
    - _Requirements: 3.6_
  - [x] 3.4 Write unit tests for all post-processors
    - _Requirements: 2.5, 3.6, 4.3_

- [x] 4. Build the streaming chat endpoint
  - [x] 4.1 Build the Chat Lambda: FastAPI behind the AWS Lambda Web Adapter in response-streaming mode on a Function URL behind CloudFront, the origin guard, per-IP and global rate limits, and the CDK construct; the same app runs as a container in `docker-compose`
    - Write a test that a request without the origin header is refused, and that streaming works in both Lambda (LWA) and container mode
    - _Requirements: 7.1_
  - [x] 4.2 Implement the request flow: availability guard (active version and archived), rate limit, length validation, parallel load and pre-check, streaming Claude call, SSE `token`/`done`/`error` events
    - _Requirements: 1.1, 1.2, 1.3, 6.1, 7.1_
  - [x] 4.3 Save the Exchange to `chat/{iso_ts}-{uuid}.json` with full metadata and the conversation ID, sign the `done` payload with an HMAC, publish `chat.exchange.saved`; send `saved:false` when the save fails
    - _Requirements: 5.1, 5.5, 5.7_
  - [x] 4.4 Write integration tests with the AI stub: stream, saved object shape (including citation snippets), 409 for archived datasets and datasets with no active version, chat answering from v1 while v2 processes, another conversation's Exchanges excluded from context, 429, save-failure path, mid-stream error
    - _Requirements: 1.1, 1.2, 1.3, 2.2, 2.6, 5.1, 5.5_

- [x] 5. Build the history and suggestions APIs
  - [x] 5.1 Implement `GET /chat/history` as a merged timeline of Exchanges and refresh markers from `dataset_versions` (pending, completed, failed), with `is_stale` flags and backward paging by a shared timestamp cursor (page size 20)
    - Write unit tests for merge order, marker states, stale flags, paging across markers, and no marker for a refresh that failed at the URL check
    - _Requirements: 5.2, 5.3, 5.6, 9.1, 9.2, 9.3, 9.4, 9.7, 9.8_
  - [x] 5.2 Implement `POST /datasets/{id}/chat/save` for retries, accepting only payloads with a valid HMAC signature
    - _Requirements: 5.5_
  - [x] 5.3 Implement `GET /chat/suggestions`, built from theme labels with templates, with general fallback questions when there are no themes
    - _Requirements: 1.4_
  - [x] 5.4 Write integration tests for history order, paging, and version labels, plus the refresh scenario: ask on v1, refresh to v2, confirm the v1 Exchange is stale, the v2 marker is present, and the next chat request excludes the v1 Exchange
    - _Requirements: 5.2, 5.3, 5.4, 9.1, 9.4, 9.6_

- [x] 6. Build the frontend ChatPanel
  - [x] 6.1 Build `HistoryPane`: virtualized list, loading earlier history on scroll, version labels, auto-scroll to the latest Exchange
    - _Requirements: 5.2, 5.3, 5.4_
  - [x] 6.2 Build citation chips with popovers from the saved snippets, and the "Outside dataset scope" tag on declined answers
    - _Requirements: 2.2, 3.4_
  - [x] 6.3 Build `ChatInput` and `VersionNote`: 1,000-character limit, Enter or Shift+Enter, empty-question guard, disabled states and messages for no active version and archived, the "Answers use v{n}" note during or after a refresh, and the 429 retry time
    - _Requirements: 1.1, 1.2, 1.3, 6.1, 6.4_
  - [x] 6.4 Build `StreamingExchange`: per-tab `conversation_id` in `sessionStorage`, SSE client, pending state, merge into history on `done`, interrupted-answer retry, unsaved warning with Retry; add other visitors' Exchanges live on `chat.exchange.saved`
    - _Requirements: 5.5, 6.2, 6.3, 7.1_
  - [x] 6.5 Build `SuggestionChips`
    - _Requirements: 1.4_
  - [x] 6.6 Build `RefreshMarker` (pending, completed, and failed states with date and time, version, trigger, and before and after review counts) and the stale-Exchange styling with the "Based on earlier data" chip and tooltip
    - _Requirements: 9.1, 9.2, 9.3, 9.4_
  - [x] 6.7 Build the history toolbar: jump to previous or next refresh marker, and collapse Exchanges from earlier versions
    - _Requirements: 9.5_
  - [x] 6.8 Refetch the latest timeline page when a `dataset.status.changed` event for this dataset starts, completes, or fails a refresh
    - _Requirements: 9.2, 9.7_
  - [x] 6.9 Write component tests for all of the above, including a live marker change from pending to completed on a mock event
    - _Requirements: 1.1, 1.3, 6.1, 6.2, 6.3, 6.4, 9.1, 9.2, 9.4, 9.5, 9.7_

- [x] 7. Build the guardrail evaluation suite
  - [x] 7.1 Create the fixture evaluation dataset, including a planted injection review, and `cases.yaml` with at least 60 labeled cases across all categories
    - _Requirements: 8.1_
  - [x] 7.2 Build the grader: deterministic checks plus an LLM-as-judge rubric; compute correct-decline, false-decline, injection-success, and citation-validity metrics; write a markdown report
    - _Requirements: 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 4.2, 8.2_
  - [x] 7.3 Add a CI job that runs on prompt-file changes and on demand, with thresholds that fail the build and a report in the job summary
    - _Requirements: 8.3, 8.4_
  - [x] 7.4 Run the evaluation with the live model, tune `system_v1.md` until every threshold passes, and record the results in the README (**needs** `ANTHROPIC_API_KEY`)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3_

- [x] 8. Write the chat performance test
  - Add `tests/perf/test_chat_latency.py`, skipped unless `PERF_LIVE_AI=1`, that asks 20 questions against a 1,000-review fixture and asserts p90 time to first token and cache-read tokens on repeat questions
  - Run it once with the live model and record the numbers in the README (**needs** `ANTHROPIC_API_KEY`)
  - _Requirements: 7.2, 7.3_

- [x] 9. Write the final main-flow E2E test
  - Open the app, check and add a dataset, wait for `updated`, ask an in-scope question (streamed, with citations), ask an out-of-scope question (explicit decline), reload (history persists), refresh the dataset (pending marker appears and the chat stays usable during processing; then a completed marker; earlier Exchanges labeled as based on earlier data, with their citation popovers still correct), archive (input disabled, history visible)
  - _Requirements: 1.3, 2.2, 3.4, 5.2, 7.1, 9.1, 9.2, 9.4_

- [x] 10. Write property-based tests for the Correctness Properties
  - Implement one property test per property in the design (Hypothesis for backend, fast-check for frontend), each tagged with its property number
  - _Requirements: 2.2, 2.5, 2.6, 4.3, 5.1, 5.2, 5.6, 6.1, 6.4, 9.1, 9.4, 9.6_
