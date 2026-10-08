# Design Document

## Overview

The processing handler consumes `processing-queue` messages and runs a pipeline of stages:
**start → collect pages → extract → dedupe → profile → metrics → complete**.
Each stage writes a progress event through `db.status.transition()` or `db.status.log_event()`, and both publish to EventBridge. Page reading is delegated to the Extraction Engine (`app.extraction`, from `review-extraction`). The Check already built an Extraction Plan for the first page, so the Worker starts with a known method and next-page rule and only calls the AI where needed. Other AI calls go through the instrumented Claude client from `platform-foundation`.

> **Page limit (from the brief's "how many pages is the max?"):** `MAX_PAGES=10` and `MAX_REVIEWS=1000` by default. At about 10–25 reviews per page, a scraped dataset holds roughly 100–250 reviews. Uploads can reach the 1,000 cap. At roughly 100–150 tokens per review, 1,000 reviews is about 100k–150k tokens, which still fits in one Claude context for chat (see guardrailed-chat for the budget rule). Both limits are configurable.

> **Scraping caveat:** Amazon and Google Maps actively block automated access and restrict scraping in their terms. The AI-first engine handles any public review page it can load; pages behind bot protection get a `wont_work` verdict at Check time, and CSV upload covers those sources.

## Architecture

```mermaid
flowchart TD
  M[SQS FIFO message<br/>group = dataset_id] --> S{version == data_version,<br/>version not completed,<br/>not archived?}
  S -- no --> X[skip]
  S -- yes --> P[transition → processing<br/>no-op if already processing]
  P --> T{source_type}
  T -- upload --> U[parse CSV with saved mapping.json<br/>apply MAX_REVIEWS keep rule]
  T -- url --> L[load plan.json + page 1]
  L --> C[collect pages<br/>extraction.next_page → capture.render<br/>SSRF-checked, ≤ MAX_PAGES, rate-limited<br/>skip pages already captured]
  C --> E[extraction.extract_page for each page]
  E --> D[dedupe across pages]
  U --> D
  D --> PR[entity profile - AI, content only]
  PR --> MT[metrics: counts, ratings, dates<br/>sentiment + themes - AI batched]
  MT --> W[write reviews/v n .json]
  W --> Z{count > 0?}
  Z -- yes --> OK[transition → updated<br/>active_version = n, metrics written]
  Z -- no --> F[transition → failed<br/>active_version unchanged]
```

The Worker is triggered by an **SQS FIFO** queue with message group ID = dataset ID and deduplication ID = `dataset_id:version`. FIFO delivers one message per dataset at a time across every Worker instance, so retries run in sequence and two instances never process the same dataset at once. Messages for different datasets run in parallel, so adding instances adds throughput.

The start guard treats `processing` for the same version as a retry and continues. Without this, the first failed attempt would set `processing` and every retry would then be skipped.

The sweeper runs every 5 minutes (EventBridge Scheduler → Lambda, or an ECS scheduled task). It claims rows with `SELECT … FOR UPDATE SKIP LOCKED`, so several sweeper runs never act on the same row. It:

- re-enqueues rows that have been `requested` for more than 5 minutes;
- marks rows `failed` that have been `processing` with no new `status_detail` event for more than 20 minutes (for example after a Lambda hard timeout), completing their `dataset_versions` row with outcome `failed`.

A DLQ consumer does the same `failed` transition for messages that exhaust their retries.

## Components and Interfaces

### Pipeline (`handlers/processing.py`)

`ProcessingHandler.handle({dataset_id, version}, meta)` runs the stages in order. Each stage is a pure function over data held in S3, which makes it easy to unit test and to rerun idempotently. Output keys include the version, so a rerun overwrites that version's objects. Pages are written to `raw/v{n}/page-{k}.html` before extraction starts, and collection skips any `k` that already exists, so a retry after a crash reuses captured pages.

Collection loop (URL datasets):

```python
html, url = load_page(1)
for k in range(2, MAX_PAGES + 1):
    nxt = extraction.next_page(html, url, plan)
    if not nxt.url: warn_if(nxt.reason_if_none); break
    assert_public_host(nxt.url)                     # from dataset-ingestion
    html, url = existing_or_capture(k, nxt.url)     # capture.render, per-host delay
    log_event(f"Captured page {k} of up to {MAX_PAGES}")
    if running_review_estimate() >= MAX_REVIEWS: break
```

Extraction calls `extraction.extract_page(html, url, plan, is_last=(k == last))` for each page and collects the `PageResult` details.

### Other AI tasks (`worker/ai/`)

All prompts include a strict instruction: *use only the provided content; do not add outside knowledge*. Outputs are requested as JSON that matches a Pydantic schema, which is validated on return.

| Task | Input | Output | Notes |
|---|---|---|---|
| `entity_profile` | Page title, page header text, plan `entity_hint`, first 30 reviews | `{name, category, description, confidence}` | |
| `classify_sentiment` | Batches of 50 reviews | `[{id, sentiment}]` | Rating used as a hint, not the final answer |
| `extract_themes` | All review texts, truncated if over budget | `[{label, mentions, lean, example_ids}]` (≤ 8) | `example_ids` must exist in the corpus |

## Data Models

### `reviews/v{n}.json`

```json
{
  "dataset_id": "uuid", "version": 1, "generated_at": "ISO",
  "entity": { "name": "Acme CRM", "category": "CRM software",
              "description": "...", "confidence": "high" },
  "pages": [ { "page": 1, "url": "…", "method": "selectors", "found": 24, "fallback": false, "discarded": 0 } ],
  "reviews": [
    { "id": "r_0001", "text": "...", "rating": 4, "date": "2026-07-01",
      "author": "J. D.", "title": "...", "sentiment": "positive", "source_page": 1 }
  ]
}
```

### `metrics` (JSONB)

```json
{
  "review_count": 212, "reported_total": 1540, "pages_captured": 10,
  "skipped": 3, "avg_rating": 3.8,
  "rating_distribution": { "1": 20, "2": 14, "3": 30, "4": 70, "5": 78 },
  "date_range": { "min": "2024-02-01", "max": "2026-09-20" },
  "sentiment": { "positive": 120, "neutral": 40, "negative": 52 },
  "themes": [ { "label": "Customer support", "mentions": 44, "lean": "negative" } ],
  "extraction": { "method": "selectors", "pages_by_selectors": 9, "pages_by_ai": 1,
                  "locator_discarded": 2, "structured_agreement": 0.95 },
  "warnings": [], "duration_ms": 48210
}
```

## Correctness Properties

Properties are tested with Hypothesis against the pipeline stages, using generated review sets and a stubbed Extraction Engine.

1. **Dedupe is idempotent and complete.** *For any* list of reviews, deduping twice SHALL equal deduping once, and the result SHALL contain no two reviews with the same normalized text, author, and date. _Validates: Requirement 3.3_
2. **Review cap.** *For any* upload and any page sequence, the stored review count SHALL never exceed MAX_REVIEWS. _Validates: Requirements 2.1, 3.4_
3. **Metrics agree with data.** *For any* review set, `review_count` SHALL equal the number of stored reviews, the rating distribution SHALL sum to the number of rated reviews, and the sentiment breakdown SHALL sum to `review_count`. _Validates: Requirements 5.1, 5.2_
4. **Active version only moves forward on success.** *For any* sequence of successful and failed versions, `active_version` SHALL equal the highest version whose outcome is `updated`. _Validates: Requirements 6.1, 6.5_
5. **Reprocessing is idempotent.** *For any* dataset version, running the pipeline twice SHALL produce identical stored outputs and one `dataset_versions` completion. _Validates: Requirement 7.3_
6. **Start guard.** *For any* combination of message version, dataset status, data version, completion state, and archive flag, the guard SHALL proceed exactly in the cases listed in Requirement 1.2. _Validates: Requirement 1.2_

## Error Handling

- **Versions and failed refreshes:** `datasets.data_version` is the latest version attempted. `datasets.active_version` is the latest version that finished `updated`. The summary, reviews table, and chat always read `active_version`. When a refresh succeeds, the Worker sets `active_version = data_version`. When it fails, `active_version` stays unchanged, so analysts keep working on the last good data. On every run, the completion step fills in the `dataset_versions` row (`completed_at`, `review_count`, `extraction_method`, `outcome`) and writes `status_detail.viability.actual = {reviews, pages, method, fallbacks}` next to the prediction. The `metrics` column is written only when a version finishes `updated`, so a failed refresh never replaces good metrics.
- Lambda timeout is 15 minutes. If collecting pages approaches 12 minutes, stop collecting and continue with what has been gathered, recording a warning. In container mode the same time budget applies, measured by the handler.
- AI output that fails JSON or schema validation is retried once with a repair prompt. If it fails again: for sentiment, the fallback is rating-based (≥4 positive, 3 neutral, ≤2 negative); for themes, they are omitted with a warning. A later page whose extraction raises `LocatorUnavailable` is skipped with a warning.
- `AIUnavailable` from the Extraction Engine or the other AI tasks is re-raised, so SQS retries with backoff. On the final attempt (from `MessageMeta.receive_count`), the version fails with "AI service unavailable — try refreshing later."
- Other unhandled exceptions propagate so that SQS retries. On the final attempt, the pipeline catches the error and moves the version to `failed`.

## Testing Strategy

- **Property-based tests** for every property above.
- **Unit tests:** start guard branches; collection loop termination (no next page, MAX_PAGES, MAX_REVIEWS, time budget, page failure, script-only pagination warning); skipping already-captured pages; upload keep rule; metric calculations including datasets without ratings or dates; sentiment fallback; stage idempotency.
- **AI tasks:** use the recorded-fixture stub. Add schema-validation tests with malformed outputs to exercise the repair and fallback paths.
- **Integration tests:** LocalStack SQS, S3, and EventBridge plus PostgreSQL, with the worker running as a container and the Extraction Engine using recorded Locator responses. Enqueue a dataset whose fixture pages and plan are in S3, then assert the status sequence `requested → processing → updated`, the `status_detail` events, the published events, and the `reviews` JSON. Include: a `selectors` dataset with one fallback page; an `ai_direct` dataset; zero reviews ending `failed`; a later-page failure ending `updated` with a warning; the sweeper re-enqueue; the sweeper failing a stale `processing` row; two concurrent sweeper runs; a retry after a first-attempt exception completing without re-capturing pages; AI unavailable through every retry ending `failed` with the right message; a failed refresh leaving `active_version` and `metrics` unchanged; the upload path including an over-limit file.
- **Isolation test:** run extraction and analysis with outbound network blocked, except for the stubbed AI client and LocalStack, to prove no external data is fetched after capture.

## Known Issues

### Collection integration tests stop at page 1 (bare-host fixture) — OPEN

Discovered 2026-10-04 while verifying an unrelated data-layer fix. The page
collection stage's integration/isolation tests (owned by this spec) fail because
collection only ever gathers **page 1**:

- `tests/integration/handlers/test_collection_int.py::test_collects_every_linked_page` → `assert [1] == [1, 2, 3]`
- `tests/integration/handlers/test_processing_pipeline_int.py::test_selectors_happy_path_with_one_fallback_page` (and siblings) → the "Captured page 2 of up to 10" progress event never appears; collection records "More reviews load only by script" instead
- `tests/integration/handlers/test_isolation_int.py::test_extraction_and_analysis_fetch_no_external_data`

**Root cause (verified in isolation, NOT a product regression).** These tests
paginate a fixture served at the **bare hostname** `http://fixtures`. The
collection loop advances via `app.extraction.next_page` (review-extraction),
which — by design (Requirement 5.3 / Property 7) — keeps a candidate next-page
URL only when it is on the **same registrable domain** as the current page,
computed with `tldextract`. A bare single-label host has no registrable domain
(`tldextract("http://fixtures") → suffix=""`), so
`pagination._same_registrable_domain("http://fixtures/…", "http://fixtures/…")`
returns `False`; every `rel="next"` / "Next" candidate is dropped and
`next_page` returns `url=None, reason_if_none="script_driven_no_url"`. Collection
therefore stops after page 1. The `SSRF_TEST_ALLOW_HOSTS=fixtures` convention
only satisfies the SSRF gate; the same-registrable-domain gate is separate and
`fixtures` fails it. Direct repro:

```python
from app.extraction import pagination as p
p._same_registrable_domain("http://fixtures/a", "http://fixtures/b")  # -> False
```

**Fix is test-side, in THIS spec's fixtures — do NOT weaken the product rule.**
Serve/address the multi-page collection fixtures under a host that HAS a
registrable domain (e.g. `http://fixtures.test/…`, or a `*.example.com` alias
pointing at the Compose `fixtures` container) and keep that host on
`SSRF_TEST_ALLOW_HOSTS`, so page N and page N+1 are same-site and `next_page`
advances. Do not relax `_same_registrable_domain` to accept bare hosts — that
would dilute the Requirement 5.3 same-site guarantee (review-extraction code,
not this spec). The affected fixtures are the inline `_page_html(...)` builders
and `_PAGE_URLS` in the three test files above.

**Harness note (environmental, applies when running these suites).** Run the
collection/processing integration tests with the SQS queues up but the worker
*consumers* (`workers-check`, `workers-processing`, `push-consumer`, `sweeper`)
**stopped** — otherwise the live Compose consumers drain the queue messages the
tests assert on, giving spurious `_drain_processing_queue() == []` failures.
`make test-int` as written brings the whole stack up with `--wait` (consumers
included), so stop the consumers before running these queue-draining tests.

See `docs/known-issues-live-stack.md` (Issue 4) for the full investigation and
repro. The prerequisite enum/UUID bind-type fix (`platform-foundation` task 15)
is already done; this is the remaining blocker for a green collection suite.

### Processing queue message field mismatch — REAL RELEASE-BLOCKING BUG

Found 2026-10-04 by running the live E2E against the Compose stack (the only
tier that exercises the real ingestion → SQS → processing hop; unit/integration
tests build the handler body or the enqueue in isolation, so neither caught it).

Symptom: a dataset is created (`POST /add` → `created`, row inserted) but never
processes — it stays `status=requested, active_version=None` forever. Every
processing message fails in `workers-processing`:
```
ValueError: processing message missing 'version':
  {'dataset_id': '...', 'data_version': 1}
  at app/handlers/processing.py:110 (ProcessingMessage.from_body)
```

Root cause: a producer/consumer field-name mismatch on the FIFO processing
queue body.
- Consumers of the message — `app/handlers/processing.py::ProcessingMessage.from_body`
  reads `body["version"]` (line ~108).
- Producers — every enqueue sends `{"dataset_id", "data_version"}`:
  `app/ingestion/service.py` (create_from_check line ~366, create_from_upload
  line ~630) and `app/datasets/refresh_service.py` (line ~192).
So `from_body` raises `KeyError('version')` → wrapped `ValueError` → the message
fails every delivery and the dataset is never analyzed. This blocks the entire
"process → summarize → chat" path end to end.

Severity: release-blocking. Nothing gets a processed dataset in a real
deployment; it only passed CI because the processing unit/integration tests hand
the handler a `{"version": n}` body directly and the enqueue tests assert the
sent body separately — no test asserts the producer and consumer agree.

Fix direction: pick ONE canonical field name for the processing message body and
make producers and consumer agree. Prefer standardizing on `data_version`
(what all three producers already send and what the row column is called):
update `ProcessingMessage.from_body` to read `body["data_version"]` (keep
tolerant handling / a clear error). Audit every producer and consumer of the
processing-queue body for the key. Owner: review-analysis owns the processing
handler; the enqueuers are dataset-ingestion — coordinate the single agreed key.
Add a test that asserts the enqueued body parses through `from_body` (a
contract test across the hop), so this cannot regress. Verify by running the
live E2E (`make e2e`, AI stubbed) with the DB migrated and consumers running: a
seeded dataset must reach `status=updated` / `active_version=1`.

Note (environment, not a bug): the live stack also requires Alembic migrations
to be applied to its Postgres (`uv run alembic -c app/db/migrations/alembic.ini
upgrade head`) — the worker containers use the real schema, unlike the
integration/scale tests which `Base.metadata.create_all`. A fresh `docker
compose` Postgres volume with no migration leaves workers crashing on `relation
"datasets" does not exist`; the deploy pipeline runs migrations as a one-off
task (platform-foundation task 7.2), and a local `make up` needs the same.

#### Update (fix applied) + remaining E2E gaps are environmental

The processing-message field mismatch is FIXED: `ProcessingMessage.from_body`
now reads `data_version` (with `version` as a backward-compatible synonym,
mirroring the DLQ handler), unit tests updated to the canonical key, and a
producer/consumer contract test added. Verified end to end against the live
Compose stack (DB migrated, workers rebuilt): a seeded dataset now goes
`requested → processing → updated` with `active_version = 1`. Lint clean;
unit/property 1447 passed.

The remaining live-E2E failures are NOT product bugs of this class — they are
local-environment/credential gaps, documented so they aren't mistaken for
regressions:
- **No WebSocket push locally.** The "live update without reload" tests
  (library row goes live; detail page processing→ready live) rely on the API
  Gateway WebSocket, which is a CDK/AWS construct with no docker-compose
  equivalent; locally the frontend only has the polling fallback. These pass
  against a deployed stack, not local `make e2e`.
- **Chat Q&A needs live AI.** `main-flow`'s `askQuestion` step requires
  `E2E_LIVE_AI=1` (or a recorded chat fixture via `make record-ai`); under the
  default stub there is no streamed answer, so it times out. This is the
  spec's documented "needs API key / needs a person" step.
