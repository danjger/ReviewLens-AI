# Known issues: live Compose stack (discovered 2026-10-03)

While running the **guardrailed-chat** main-flow E2E against a freshly rebuilt
`make up` stack, the live ingestion → capture → processing pipeline failed to
produce a processed dataset. Investigation found **three independent defects**,
all stemming from one root cause: **rebuilding the Docker images today pulled
newer dependency versions than the code was written against** (the `pyproject`
floors were open-ended, so the images floated above the `uv.lock`-resolved
versions).

None of these are in the `guardrailed-chat` spec — its own code and tests
(244 unit/property/integration tests) pass. They live in
`platform-foundation` (images, capture, consumer) and `dataset-ingestion`
(the Check → Add flow). Fix them in their own spec sessions, in the order
below (they form a dependency chain).

The E2E test itself (`e2e/tests/main-flow.spec.ts`) is correct and skips
cleanly today; it will pass once the pipeline works — no change needed there.

---

## Suggested order

1. **Issue 1 (selectolax)** — already fixed; keep the pin.
2. **Issue 2 (capture greenlet crash)** — the root blocker. Fix first.
3. **Issue 3 (Add inserts nothing)** — diagnose AFTER capture works; it may be
   a symptom of a failed capture rather than a separate bug.

A cheaper framing for 1 + 2: a single **dependency-pinning pass** in
`platform-foundation` (align the `pyproject` floors and the worker Dockerfile
with the known-good versions in `uv.lock`) likely clears both.

---

## Issue 1 — selectolax 1.0 removed the `.parser` (Modest) backend  ✅ FIXED

**Spec:** platform-foundation (backend deps).
**Symptom:** `workers-check` and `workers-processing` containers exited (1) on
startup:

```
ImportError: Modest backend is deprecated since selectolax 1.0. ... Please use
lexbor backend instead: `from selectolax.lexbor import LexborHTMLParser`.
  File ".../app/ingestion/prescan.py", line 27, in <module>
    from selectolax.parser import HTMLParser, Node
```

**Root cause:** `pyproject.toml` had `selectolax>=0.3.0`; the image built
`selectolax==1.0.0`, which raises on `import selectolax.parser`. 8 backend files
use `from selectolax.parser import ...` (prescan, extraction/*, handlers/processing).
`uv.lock` had actually resolved `0.4.13` (which still has `.parser`).

**Fix applied (this session):** pinned `pyproject.toml` →
`"selectolax>=0.3.0,<1.0"`, re-ran `uv lock` (resolves 0.4.13), rebuilt the
workers image. Workers now start healthy. Keep this pin, OR migrate all 8 files
to `from selectolax.lexbor import LexborHTMLParser` (larger, review-extraction
work — API differs).

---

## Issue 2 — Playwright capture crashes with a greenlet thread error  ❌ OPEN

**Spec:** platform-foundation (`app/capture/engine.py`, `app/consumer.py`).
**Symptom:** every live page capture fails; no new capture objects are written.
`docker compose logs workers-check`:

```
INFO:app.capture.engine:Launched headless Chromium for capture
ERROR:__main__:handler failed for message <id> on check (attempt 1/3)
Traceback (most recent call last):
    capture = capture_engine.render(final_url, prefix)
  File "/var/task/app/capture/engine.py", line 248, in render
greenlet.error: cannot switch to a different thread (which happens to have exited)
```

**Suspected root cause:** Playwright's **sync API is not thread-safe**.
`app/capture/engine.py` launches ONE browser per process via
`sync_playwright().start()` and caches it in a module global `_browser`
(`_get_browser()`), intending to reuse it across messages. But the consumer
(`app/consumer.py`) runs work alongside a per-message **heartbeat thread**
(`threading.Thread(target=_run_heartbeat, ...)`), and a sync Playwright object
bound to the greenlet/thread that created it cannot be driven after the thread
context changes — producing `cannot switch to a different thread`. This is a
classic sync-Playwright-across-threads failure, almost certainly surfaced (or
worsened) by a newer Playwright/greenlet version from the rebuild.

**Where to look / likely fixes:**
- `app/capture/engine.py::_get_browser` (module globals `_browser`,
  `_browser_lock`, `sync_playwright().start()`), `render()` at line ~248.
- `app/consumer.py::run_poller` + `_run_heartbeat` (the heartbeat thread).
- Options: pin Playwright + greenlet in `pyproject.toml`/`Dockerfile.workers`
  to the versions the capture engine was written against (check `uv.lock`);
  and/or create the Playwright object on the SAME thread that calls `render`
  (don't cache a browser created on a different thread), or isolate capture in
  a dedicated thread/subprocess that owns its own Playwright instance for its
  lifetime. Verify against the capture integration tests
  (`backend/tests/integration/capture/`).

**Repro:**
```bash
make up            # with the selectolax pin from Issue 1
# POST a check for a fixture page, then watch:
docker compose logs -f workers-check   # greenlet.error on each capture
```

---

## Issue 3 — Check → Add returns `created` but inserts no dataset  ❌ OPEN

**Spec:** dataset-ingestion (`app/ingestion/service.py::create_from_check`,
`app/ingestion/api.py` add endpoint).
**Symptom:** a limited/will_work Check followed by Add returns HTTP 200 with
`{"outcome":"created","dataset_id":null}`, but **no row exists** in the
`datasets` table afterward:

```bash
# After a successful-looking Add:
docker compose exec -T postgres psql -U reviewlens -d reviewlens -t \
  -c "select count(*) from datasets;"
# -> 0
```

The API log shows the `POST /add` returning 200 with no error logged.

**Suspected root cause (diagnose AFTER Issue 2 is fixed):** `create_from_check`
copies the Check's capture objects (page/plan/snapshot) from S3 and then inserts
the `datasets` + v1 `dataset_versions` rows, with failure-cleanup on error. With
capture crashing (Issue 2), the capture objects for a *new* run may be missing
or stale, so the copy/insert path can bail — but it is returning `created` with
a null `dataset_id` rather than a failure outcome, which is itself a bug (a
`created` outcome must carry the new `dataset_id`, per the code at
`service.py` `return AddResult(item.item_id, "created", dataset_id=dataset_id)`).
Determine whether the insert is silently rolled back / an exception is swallowed,
or whether an idempotency/claim path (`_reapplied_outcome`, which returns no
`dataset_id`) is being hit unexpectedly.

**Where to look:**
- `app/ingestion/service.py`: `_add_one`, `create_from_check`, `_insert_dataset`,
  `_reapplied_outcome`, the `IntegrityError` / failure-cleanup branches.
- Confirm with the dataset-ingestion integration tests
  (`backend/tests/integration/ingestion/`).

**Repro:** see the `curl` sequence in the session; in short — POST
`/api/ingest/checks` for a fixture URL, poll `/api/ingest/checks/{id}` to a
terminal verdict, POST `/api/ingest/checks/{id}/add` with
`confirm_limited:true`, then check the `datasets` table (0 rows).

---

## After the fixes: re-verify guardrailed-chat end-to-end

No guardrailed-chat change is needed. Once the pipeline works:

```bash
make up                                   # stack healthy incl. workers
cd e2e && npx playwright install chromium # first time only
E2E_LIVE_AI=1 make e2e                     # runs the live main-flow E2E
```

The backend uses the live model from `.env` (chat `claude-sonnet-5-5`,
precheck `claude-haiku-4-5-20251001`). The E2E currently SKIPS (annotated
`needs-record-ai` / `deferred`) because seeding a processed dataset fails on
Issues 2/3; it will run fully once those are fixed.

---

## Issue 4 — Collection stops at page 1 in integration tests (bare-host fixture)  ❌ OPEN

**Spec:** dataset-ingestion / review-analysis (the `handlers/` integration
tests), NOT review-extraction product code and NOT platform-foundation.
Discovered 2026-10-04 while verifying the task-15 enum/UUID fix.

**Symptom:** after task 15 cleared the uuid/enum errors, ~ a dozen integration
tests still fail because collection only ever gathers **page 1**:
```
tests/integration/handlers/test_collection_int.py::test_collects_every_linked_page
    assert [1] == [1, 2, 3]
tests/integration/handlers/test_processing_pipeline_int.py::test_selectors_happy_path_with_one_fallback_page
    assert 'Captured page 2 of up to 10' in ['processing',
        'More reviews load only by script; collected pages may be incomplete', 'updated']
tests/integration/handlers/test_isolation_int.py::test_extraction_and_analysis_fetch_no_external_data
```

**Root cause (verified in isolation):** these tests paginate a fixture served at
the **bare hostname** `http://fixtures` (and some use `http://localhost`).
`app.extraction.pagination.next_page` keeps a candidate next-page URL only when
it is on the **same registrable domain** as the current page (Requirement 5.3),
computed with `tldextract`. A bare single-label host has **no registrable
domain** (`tldextract("http://fixtures") → suffix=""`), so
`_same_registrable_domain("http://fixtures/…", "http://fixtures/…")` returns
`False`, every candidate is dropped, and `next_page` reports
`reason_if_none="script_driven_no_url"` even though the fixture HTML has a real
`rel="next"` + "Next" anchor. Collection therefore stops after page 1.

Reproduced directly:
```python
from app.extraction import next_page
# fixture HTML with <link rel="next" href="http://fixtures/...?page=2"> + <a>Next ›</a>
next_page(html, "http://fixtures/collection/reviews?page=1", plan)
#  → url=None, rule_used="none", reason_if_none="script_driven_no_url"
from app.extraction import pagination as p
p._same_registrable_domain("http://fixtures/a", "http://fixtures/b")  # → False
```

**This is NOT a product regression.** The same-registrable-domain rule is the
intended design (`pagination.py` module docstring; Requirement 5.3; Property 7),
and it already tolerates `tldextract` version drift
(`top_domain_under_public_suffix` → `registered_domain` fallback). The defect is
in the **test fixtures' choice of a bare-hostname host**, which the same-site
rule correctly refuses — so pagination can never advance in those tests. The
capture tests pass because they render a single page and never paginate.

**Suggested fix (test-side, in the owning spec):** serve/address the multi-page
collection fixtures under a host that HAS a registrable domain (e.g.
`http://fixtures.test/…` or a `*.example.com` alias pointing at the Compose
`fixtures` container) so `_same_registrable_domain` treats page N and page N+1
as same-site, and keep that host on the SSRF allowlist. Do NOT weaken
`_same_registrable_domain` in product code to accept bare hosts — that would
dilute Requirement 5.3's same-site guarantee. Then re-run
`tests/integration/handlers/test_collection_int.py`,
`test_processing_pipeline_int.py`, and `test_isolation_int.py` with the worker
consumers STOPPED (see the note below).

**Harness note (separate, environmental):** run these integration suites with
the queues up but the worker *consumers* (`workers-check`, `workers-processing`,
`push-consumer`, `sweeper`) **stopped** — otherwise the live containers consume
the SQS messages the tests expect to drain, causing spurious
`_drain_processing_queue() == []` failures. `make test-int` as written brings
the whole stack up with `--wait`, including the consumers; running the
`handlers/` and `ingestion/` queue-draining tests needs the consumers down.
