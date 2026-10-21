# Verification — Fix #10: wire local realtime WebSocket under `make e2e`

Branch: `fix/local-realtime-ws` (worktree
`/Users/dan/kiro/experiments/ReviewLens-AI/.worktrees/wire-local-realtime`).

All commands were run from the worktree. This records the exact commands,
pass/fail counts, and the evidence a reviewer needs WITHOUT re-running the
suites.

---

## What the fix does (and why production is unaffected)

### 1. The #10 wiring — a LOCAL-ONLY WebSocket path (compose/config only)

- `backend/app/core/config.py` — added `ws_local_relay_url` (env
  `WS_LOCAL_RELAY_URL`), **empty by default**. Empty in AWS, so the deployed
  push Lambda keeps the production `apigatewaymanagementapi.postToConnection`
  path. No production validator or `ws_api_endpoint` changed.
- `backend/app/realtime_dev.py` (NEW, **LOCAL/DEV ONLY**) — a compose-only
  FastAPI relay: `WS /realtime` (browser connects), `POST /broadcast` (push
  consumer fans out), `GET /healthz`. Holds an in-process socket set — the
  documented tech.md dev exception, isolated to this module and its own
  container. Never a CDK resource or a Lambda command; `mypy --strict` clean.
- `backend/app/handlers/push.py` — `handle()` branches on `ws_local_relay_url`:
  when set (Compose), HTTP-POST the already-built frame to `{relay}/broadcast`;
  when empty (AWS), the EXISTING `postToConnection` fan-out runs **byte-for-byte
  unchanged** (same frame shape, `GoneException` cleanup, thread pool). A relay
  POST failure is logged and swallowed (a push failure never fails processing).
- `docker-compose.yml` — added the `realtime-dev` service (`:8002`, `/healthz`
  healthcheck); set `WS_LOCAL_RELAY_URL: http://realtime-dev:8002` and
  `depends_on: realtime-dev (service_healthy)` on `push-consumer`. No change to
  api/chat/workers/sweeper behavior.
- `infra/localstack-init/01-provision.sh` — added EventBridge rule
  `reviewlens-broadcast-to-push` on `reviewlens-events` matching
  `source=reviewlens` + detail-types `dataset.status.changed` / `check.updated`,
  targeting `push-queue` (mirrors `BROADCAST_DETAIL_TYPES` in `push.py` and the
  AWS `BroadcastToPushQueue` rule). Sentinel write kept LAST.
- `frontend/vite.config.ts` — added a `/realtime` → `ws://localhost:8002`
  (`ws: true`) proxy. **Dev-server only**; production bakes `VITE_WS_URL` and
  never uses this proxy.
- `e2e/README.md` — documented the local realtime path and the serial-worker
  decision (below).

### 2. Live-push CONVERGENCE fixes the working socket exposed (frontend reducers)

Wiring a real socket locally exposed latent convergence gaps in the
dataset-library/ingestion reducers (previously masked because no socket
delivered frames locally). Fixed in `frontend/src/hooks/realtimeReducers.ts`:

- **List badge (issue #10 headline):** `applyDatasetStatusChanged` now settles a
  known row's status badge IN PLACE on a TERMINAL frame (`updated`/`failed`) by
  deriving the server's `display_state` client-side (`terminalDisplayState`),
  rather than invalidating `['datasets']`. The non-terminal in-place patch and
  the unknown-id invalidation are UNCHANGED. **Why derivation, not a list
  refetch:** the list query is keyed on the constant `['datasets']` with the
  filter held only in the `useDatasets` queryFn, so any list refetch races an
  active Library search filter and re-shows sibling rows (breaks the #12
  isolation contract). Deriving in place settles the badge with NO refetch, so
  there is no filter race. A **mandatory STRONG pinning test** asserts
  `terminalDisplayState` agrees with the server's single source of truth,
  `backend/app/datasets/library.py::derive_display_state`, across the FULL
  terminal domain (updated/failed × active/none), with the delegated case
  (`updated`+no-active → the server's non-terminal `processing` fallback)
  asserted explicitly, plus a vocabulary guard — so any future change to the
  server mapping turns the test red (mitigates source-of-truth drift).
- **Check session:** `applyCheckUpdated` now, after the in-place patch, refetches
  the full `['check', id]` session when the pushed item reached a TERMINAL state.
  The `check.updated` frame carries only `{state, verdict}`; the in-place patch
  flips `state` to terminal, which stops `useCheck`'s poll before the complete
  verdict detail (reasons, sample reviews, the "already tracked → refresh"
  `TrackedNote` linkage) arrives. The refetch makes the check path converge live
  with no reload. There is no filter to clobber on `['check', id]`, so this
  invalidation is clean.

### 3. Deliberate deviation recorded: the property-test oracle

`realtimeReducers.property.test.ts`'s INDEPENDENT oracle (`expectedRow`) was
updated to also fold `display_state` — because the reducer's output legitimately
changed (it is now a sticky derived field, set only by a terminal frame and
never cleared, exactly like `review_count`). Leaving the oracle stale would make
it FALSELY fail. **Correctness Property 4 (convergence) is still asserted** and
the reducer's exact-key `['datasets']` `getQueryData`/`setQueryData` in-place
coupling is UNTOUCHED; the oracle change is the minimal mirror of the new sticky
field, nothing more.

### 4. Serial-worker decision (option A)

`e2e/playwright.config.ts` now sets `workers: 1` **unconditionally** (was
`process.env.CI ? 1 : undefined`). CI already forced one worker, so this makes
LOCAL match CI rather than diverge. **Why:** the suite drives one shared
backend/DB with no per-test namespacing, and the now-working relay broadcasts
every dataset's live frames to EVERY open tab (mirroring production's API
Gateway WS "broadcast to all"). Under PARALLEL workers that is a race — one
test's new dataset streams into another test's observing tab, where the correct
unknown-id live refetch repopulates the list and defeats that test's
`tracked-search` scoping (the #12 shared-single-stack problem). This is a
test-isolation artifact, NOT a product defect (a new dataset from another user
SHOULD appear live). Serial execution is the honest fix; documented in the e2e
README. `scopeLibraryTo` and all #12 assertions are UNTOUCHED.

---

## What was NOT changed (production-safety checklist)

- `backend/app/realtime/connect.py`, `disconnect.py`, `connections.py` — unchanged.
- `push.py` AWS branch (`apigatewaymanagementapi.post_to_connection` against
  `ws_api_endpoint`) runs whenever `WS_LOCAL_RELAY_URL` is empty — the deployed
  push Lambda case. Byte-for-byte unchanged.
- `infra/lib/realtime-stack.ts` and all CDK stacks — unchanged; the relay is a
  compose-only service, never a CDK resource or Lambda command.
- `config.py` `ws_api_endpoint` and all production validators — unchanged.
- Frontend `resolveWsUrl` precedence — unchanged; production uses the baked
  `VITE_WS_URL`. The `/realtime` vite proxy is dev-server only.
- `useDatasets.ts` — byte-identical to baseline (the exploratory ref-backed
  filter was reverted; option 3 needs no list refetch, so it was dead code).
- #12 files `e2e/support/live.ts`, `library.spec.ts`, `ingestion-summary.spec.ts`
  — `scopeLibraryTo` and every #12 assertion UNTOUCHED.
- `realtimeReducers.test.ts` / `.property.test.ts` — exact-key coupling and
  Correctness Property 4 preserved; only the property oracle folds the new
  sticky `display_state` field (see §3).

---

## Static checks — `make lint` (CLEAN)

```
make lint
  ruff check .            → All checks passed!
  ruff format --check .   → 285 files already formatted
  mypy app/               → Success: no issues found in 98 source files   (--strict; incl app/realtime_dev.py)
  eslint . --max-warnings 0 → clean
  tsc -b                  → clean
```

## Frontend unit + property tests (CLEAN)

```
cd frontend && npm test
  → Test Files 61 passed (61) · Tests 515 passed (515)
```
Includes the new terminal-derivation pinning suite and the updated (still
Property-4-asserting) `realtimeReducers.property.test.ts`.

```
cd backend && uv run pytest tests/unit/test_realtime_dev.py tests/unit/handlers/test_push_handler.py -v
  → 12 passed
```
(relay: healthz, broadcast→1 client, no-client no-op, broadcast→2 clients;
push: local-relay branch POSTs /broadcast and does NOT call postToConnection; a
relay POST failure is swallowed.)

---

## Stack bring-up + the known gotcha

```
docker compose down -v && docker compose up -d --wait   → all 9 services healthy
  (api, chat, fixtures, localstack, postgres, push-consumer, realtime-dev,
   workers-check, workers-processing)
```
EventBridge rule provisioned by the LocalStack init:
```
aws --endpoint-url=http://localhost:4566 --region=us-east-1 events list-rules --event-bus-name reviewlens-events
  → reviewlens-broadcast-to-push, ENABLED,
    pattern {"source":["reviewlens"],"detail-type":["dataset.status.changed","check.updated"]}
aws ... events list-targets-by-rule --rule reviewlens-broadcast-to-push ...
  → Target push-queue → arn:aws:sqs:us-east-1:000000000000:push-queue
```
Migrations applied from the host (compose has no auto-migrate):
```
cd backend && DATABASE_URL="postgresql+psycopg://reviewlens:reviewlens@localhost:5432/reviewlens" \
  uv run alembic -c app/db/migrations/alembic.ini upgrade head   → head 0002_html_upload_source_type
```
Gotcha gate — a fixture Check reaches a verdict before any E2E run (so seeding
works, not INTERNAL_ERROR/SKIP):
```
POST /api/ingest/checks {"urls":["http://fixtures/extraction/plain_list/?e2e=warm"]} → check_id; poll → state=done
```
NOTE: the FIRST Check after a fresh `docker compose up` is slow (cold vite
compile + cold worker + first AI-stub call). A single cold test can falsely
time out; warm the stack with one probe Check (above) before trusting a run.
(An earlier intermittent LocalStack `QueueDoesNotExist` on SendMessage to the
FIFO processing queue was cleared by a clean `docker compose down -v && up
--wait`; it is a LocalStack-community-edition artifact, not from this change.)

---

## PRIMARY PROOF — `E2E_REALTIME=1 realtime.spec.ts --repeat-each=3`

```
cd e2e && E2E_REALTIME=1 npx playwright test realtime.spec.ts --repeat-each=3
  → 9 passed (3 tests × 3)
```
- "the SPA opens a live WebSocket channel" — PASS. The `__wsOpened` probe sees
  the browser socket OPEN at `ws://localhost:5173/realtime`, proxied by vite to
  the compose `realtime-dev` relay; URL matches `ws(s)://`.
- "a new dataset appears and settles live, with no reload" — PASS, consistently
  ~3.6s (was timing out at 60s before the convergence fix; the ~50% race is
  gone). A dataset seeded from a second context APPEARS and its status badge
  leaves `processing` purely via live push, NO reload.
- "harness: realtime gating is explicit" — PASS.

Push path confirmed end-to-end at runtime (logs): publisher → bus → new rule →
push-queue → push-consumer `POST http://realtime-dev:8002/broadcast 200` →
relay → browser. Server end-state of the seeded dataset: `status=updated,
display_state=ready`.

## Regression — FULL `make e2e` (now serialized), run to completion TWICE

`make e2e` = `cd e2e && npx playwright test` (default, `E2E_REALTIME` unset,
`workers: 1` per the config change). On a freshly provisioned + warmed stack:

```
Run 1:  12 passed, 3 skipped, 1 failed
Run 2:  12 passed, 3 skipped, 1 failed   (accumulated state from run 1)
```
- The ONLY failure in each run is `main-flow.spec.ts:313` — the guardrailed-chat
  scope-guard step that is AI-dependent (`needs-record-ai`), pre-existing and
  untouched (same failure documented in the #12 verification). It was NOT made
  to pass (out of scope).
- ALL `library.spec.ts` (incl :244 moves-through-statuses, :322 archive/restore,
  :482 re-submit-refreshes) and `ingestion-summary.spec.ts` are GREEN under
  accumulated state — the runs that were failing before the fix now pass, and
  are stable across two consecutive runs.
- Skipped (3): environment-gated (realtime needs `E2E_REALTIME=1`; main-flow
  placeholders) — pre-existing, not regressions.

## No reintroduced flake — serialized #12 pair, repeat-each=2

```
cd e2e && npx playwright test tests/library.spec.ts tests/ingestion-summary.spec.ts --repeat-each=2
  → 12 passed
```

---

## Reviewer confirmations

- (i) Production convergence unchanged: the final badge is identical, now
  reached from the live terminal frame via `terminalDisplayState`, which
  PROVABLY matches the server's `derive_display_state` (pinning test across the
  full terminal domain). No list refetch, so no extra `/api/datasets` traffic in
  production and no visual/final-state difference.
- (ii) Reducer exact-key `['datasets']` in-place-patch coupling UNTOUCHED;
  Correctness Property 4 still asserted (the property oracle change is the
  minimal sticky-field mirror of the new derived `display_state`, nothing more).
- (iii) #12 isolation intact: `scopeLibraryTo` and all #12 assertions UNTOUCHED;
  `useDatasets.ts` byte-identical to baseline.
- (iv) Production `postToConnection` path byte-for-byte unchanged; the relay is
  local-only and OFF by default in AWS (`WS_LOCAL_RELAY_URL` empty).
- CI `workers` unchanged (still 1); local now matches CI (`workers: 1`
  unconditional), documented in `e2e/README.md` and §4 above.
