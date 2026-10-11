# Known issues: live Compose stack — SUPERSEDED (see platform-foundation tasks)

> **Status (updated 2026-10-03): mostly fixed. Do not act on the original
> three-issue list below without checking the authoritative source first:
> `.kiro/specs/platform-foundation/tasks.md` tasks 14–17.** A platform-foundation
> session has since fixed the root causes; this file is kept only as a pointer.

## What was found originally
Running the guardrailed-chat live E2E against a freshly rebuilt `make up` stack
exposed a broken ingestion → capture → processing pipeline. The root cause was
dependency drift: the Docker images were built from the open-ended `pyproject`
floors instead of `uv.lock`, so a rebuild floated above the locked versions.

## Current status (authoritative: platform-foundation/tasks.md)
- **Issue 1 — selectolax 1.0 broke the workers.** FIXED. `pyproject` pinned
  `selectolax<1.0`; more fundamentally, **task 14** made `Dockerfile` /
  `Dockerfile.workers` install the locked set (`uv sync --frozen`) so nothing
  floats (locked: playwright 1.63.0, greenlet 3.5.6, selectolax 0.4.13).
- **Issue 2 — Playwright `greenlet.error` in capture.** FIXED in **task 14**
  (capture now owns its Playwright lifecycle on the thread that drives `render`).
- **Issue 3 — Add returned `created` but inserted no `datasets` row.** FIXED in
  **task 15**: the real cause was NOT a capture symptom but a `str`→`uuid`/enum
  **bind-type mismatch** in raw SQL (a stricter psycopg from the rebuild);
  `app/jobs/sweep.py` and `app/ingestion/service.py` now use explicit
  `CAST(... AS uuid)` / `CAST(... AS dataset_status)`. Add now creates a dataset
  that reaches `ready`.
- Also fixed along the way: moto table-collision in the test suite (task 13),
  status-event time-ordering (task 16), Aurora engine version + deploy OIDC
  (tasks 18–19).

## The remaining blocker for the guardrailed-chat live E2E
Seeding is unblocked — the 9 E2E pipeline tests now RUN the real pipeline
(the local fixture-host/SSRF mismatch and a local rate-limit cap were fixed on
the test/compose side; see **task 17**). Two things still gate a fully green
`main-flow` run, and NEITHER is a guardrailed-chat code bug:

1. **SPA WebSocket not wired under the local vite dev server** (no `VITE_WS_URL`,
   `/realtime` not proxied), so live-update assertions time out. Tracked in
   **GitHub issue #10** (dataset-library / realtime). Platform-foundation task 17
   is intentionally left open until #10 lands.
2. **`main-flow`'s chat step needs a recorded Locator/chat response or
   `E2E_LIVE_AI=1`.** With `E2E_LIVE_AI=1` and the live key/models in `.env`
   (chat `claude-sonnet-5-5`, precheck `claude-haiku-4-5-20251001`) the chat
   step runs live.

## Re-running the guardrailed-chat live E2E
No guardrailed-chat change is needed; the test (`e2e/tests/main-flow.spec.ts`)
is correct. Once issue #10 is resolved (SPA WebSocket under vite):

```bash
make up                                   # stack healthy incl. worker consumers
cd e2e && npx playwright install chromium # first time only
E2E_LIVE_AI=1 make e2e                     # or: npx playwright test main-flow
```
