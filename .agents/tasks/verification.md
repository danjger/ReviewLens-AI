# Verification — Fix #12: E2E flakiness under a shared single-stack DB

Branch: `fix/e2e-shared-db-flake` (worktree
`/Users/dan/kiro/experiments/ReviewLens-AI/.worktrees/fix-e2e-shared-db-flake`).

Test-only change. Files edited (nothing under `frontend/src` or `backend/app`):

- `e2e/support/live.ts` — added shared helpers `searchTermFor`,
  `scopeLibraryTo`, `archiveAllCreated` (and a `APIRequestContext` type import).
  `settleViaReload` / `expectVisibleViaReload` are UNCHANGED, so the change #11
  reload-vs-no-reload distinction (`allowReload: !liveAi`) is preserved exactly.
- `e2e/tests/library.spec.ts` — scoped every presence/absence/status assertion
  to the test's OWN dataset (via `data-dataset-id` + the `tracked-search`
  filter), ELIMINATED the whole-list row-count assertion in scenario 4
  (`rowsBefore`/`rowsAfter`/`toBeLessThanOrEqual`) in favour of the
  sibling-independent "exactly one row for this id", and added a per-test
  `afterEach` teardown that archives everything the test created.
- `e2e/tests/ingestion-summary.spec.ts` — added the same `afterEach` teardown,
  documented that each `goto('/datasets/{id}')` is a full load that starts the
  TanStack Query cache / useRealtime fresh (so the Library list fetched by the
  initial `goto('/')` can't bleed in), and rewrote scenario 1's post-filter
  reviews assertion to poll a single COHERENT settled state (empty state, OR
  rows where every rating cell shows the chosen value) instead of racing the
  `rating=5` refetch with a `<tbody>` visibility check and a stale per-row
  `count()` loop.

Nothing weakens a deployed-run no-reload live-push assertion: `scopeLibraryTo`
filters the list with Playwright's built-in retrying assertions (no reload), and
every status/live assertion keeps its `allowReload: !liveAi` wiring.

## Environment

- Docker 29.8.1, Compose v5.5.1. `make up` from the worktree brought up the full
  stack (postgres, localstack, fixtures, api, chat, workers-check,
  workers-processing, push-consumer, sweeper); all healthy. Compose sets
  `AI_STUB`/`SSRF_TEST_ALLOW_HOSTS=fixtures`; `E2E_LIVE_AI` NOT set (stub mode).
- Migrations applied from the host (the compose stack has no auto-migrate):
  `cd backend && DATABASE_URL="postgresql+psycopg://reviewlens:reviewlens@localhost:5432/reviewlens" uv run alembic -c app/db/migrations/alembic.ini upgrade head`
  → head `0002_html_upload_source_type`.
- `GET http://localhost:8000/api/datasets?archived=false` → 200.
- `cd e2e && npm install && npx playwright install chromium`.
- `cd frontend && npm install` (Playwright's webServer runs the vite dev server).
- Playwright owns the vite dev server via its `webServer` config (as `make e2e`
  does); runs are `E2E_LIVE_AI` unset (stub), `--workers=1`.

## 1. Reproduce the flake FIRST (before the fix)

Command (from `e2e/`):

```
for i in 1 2 3 4 5; do npx playwright test tests/library.spec.ts tests/ingestion-summary.spec.ts --workers=1; done
```

Result — the flake reproduced:

- Run 1: **1 failed, 5 passed** — `ingestion-summary.spec.ts` scenario 1
  ("open a processed dataset: … filtered reviews") failed.
- Runs 2–5: 6 passed each.

A single test failing on only SOME runs of the identical serialized command, with
the per-file execution order differing between runs, is the order/timing-dependent
cross-test flake described in issue #12.

## 2. Prove the flake is gone (after the fix)

`cd e2e && npx tsc --noEmit` → exit 0 (the binding TS check for the e2e project;
`make lint`'s frontend leg does not cover `e2e/`). Re-run after every edit → clean.

Per-spec, serialized, repeat-each=3 (from `e2e/`):

```
npx playwright test tests/library.spec.ts --workers=1 --repeat-each=3
  → 12 passed (4 tests × 3)
npx playwright test tests/ingestion-summary.spec.ts --workers=1 --repeat-each=3
  → 6 passed (2 tests × 3)
```

The two affected specs TOGETHER, serialized, repeat-each=3 — the exact invocation
the issue asks for — run twice consecutively:

```
npx playwright test tests/library.spec.ts tests/ingestion-summary.spec.ts --workers=1 --repeat-each=3
  → run A: 18 passed (2.2m)
  → run B: 18 passed (2.6m)
```

36 total test executions across the two combined runs, **0 failures** — the
previously nondeterministic cross-test failure is gone. (Two earlier combined
`--repeat-each=3` passes were also observed while iterating, before two
remaining races in ingestion-summary scenario 1 were fixed; the two runs above
are the final, post-fix evidence.)

## 3. Full-suite regression (`make e2e`)

```
make e2e   # cd e2e && npx playwright test  (all specs, stub AI)
  → 12 passed, 3 skipped, 1 failed
```

- **Skipped (3):** environment-gated specs (e.g. `realtime.spec.ts` needs
  `E2E_REALTIME=1`; `main-flow` placeholders) — pre-existing, not a regression.
- **Failed (1):** `main-flow.spec.ts:369` — the guardrailed-chat scope-guard
  assertion `expect(declined).toHaveAttribute("data-scope", "declined")` got
  `"in_scope"`. This is the AI-dependent step the task flags as `needs-record-ai`
  (a genuine "declined" verdict needs a recorded/live AI answer; the default stub
  can't produce one). **It is pre-existing and NOT caused by this change:**
    - `git status` shows only `e2e/support/live.ts`,
      `e2e/tests/ingestion-summary.spec.ts`, `e2e/tests/library.spec.ts` modified;
      `main-flow.spec.ts` is untouched.
    - `main-flow.spec.ts` imports only `fixtureUrl` and `liveAi` from
      `support/env.ts` (which this change does not touch) — it never uses any of
      the helpers added to `support/live.ts`.
    - Running `npx playwright test tests/main-flow.spec.ts --workers=1` in
      isolation reproduces the identical `data-scope="in_scope"` failure, so it
      fails independent of this change.

  Per the task, the live-AI guardrailed-chat answer was NOT made to work; it was
  only confirmed not to regress.

No previously-passing spec regressed.

---

## 4. Follow-up: cosmetic tidy-up + independent re-verification (on `main`)

Addressing the reviewer's one non-blocking note: `scopeLibraryTo` accepted a
`SettleOptions` object but ignored the `allowReload` arg callers passed, which
was misleading (scoping never reloads — a reload clears the search box).

Change (test-only, on `main` after the fix merged):

- `e2e/support/live.ts` — `scopeLibraryTo` now takes its own narrow
  `ScopeOptions` type (`{ timeout?: number }`) instead of `SettleOptions`, so
  there is no `allowReload` knob to pass. Docstring updated to state scoping is
  always no-reload and the live-push-vs-reload distinction stays solely a
  `settleViaReload` concern.
- `e2e/tests/library.spec.ts` — dropped the dead `allowReload: !liveAi` arg from
  all five `scopeLibraryTo(...)` call sites; fixed one misleading comment that
  described scoping as "reload locally, live refetch deployed". The
  `allowReload: !liveAi` wiring on `settleViaReload` / `expectVisibleViaReload`
  is UNCHANGED (those genuinely reload), so the change #11 distinction is intact.
  `liveAi` is still imported and used (skipNoSeed + the settle calls).

Re-verification (fresh environment, from scratch):

- `cd e2e && npx tsc --noEmit -p tsconfig.json` → exit 0 (E2E TSC CLEAN).
- A stale compose project from the worktree run (`fix-e2e-shared-db-flake`) was
  still holding host port 9090; removed it (`docker compose -p
  fix-e2e-shared-db-flake down`). A half-created `reviewlens-ai` stack from a
  failed first `up` had the `api` container unable to resolve `localstack`
  (`Name or service not known`) → `POST /api/ingest/checks` returned
  INTERNAL_ERROR (botocore `EndpointConnectionError` to
  `http://localstack:4566/`) and every library/ingestion-summary test SKIPPED
  via `skipNoSeed` (seeding returned null). Fixed with a clean
  `docker compose down && up -d --wait`, then waited for LocalStack
  "provisioning complete"; afterwards `api → http://localstack:4566/_localstack/health`
  = 200 and a probe Check for `plain_list` returned verdict `limited` (addable),
  so seeding works and the tests RUN.
- Both affected specs TOGETHER, serialized, `--repeat-each=3`, run twice:
  - run A: **18 passed** (2.3m)
  - run B: **18 passed** (2.6m)
  36 executions, 0 failures.
- Full `make e2e`: **12 passed, 3 skipped, 1 failed** — the single failure is
  the same pre-existing `main-flow.spec.ts` guardrailed-chat `needs-record-ai`
  step (untouched file, AI-dependent), not a regression from this tidy-up.
