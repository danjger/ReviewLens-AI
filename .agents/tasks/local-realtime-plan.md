# Implementation Plan — Fix GitHub issue #10: wire up local realtime WebSocket

All paths are absolute under the worktree
`/Users/dan/kiro/experiments/ReviewLens-AI/.worktrees/wire-local-realtime`.
Relative paths must NOT be used — they land in the parent workspace.

---

## Investigation findings (file/line evidence)

The issue is real and, importantly, has **three** local gaps, not one. Nothing
serves a browser-reachable WebSocket locally today, and even if it did, no
events would reach it. Evidence:

1. **The browser targets `ws://localhost:5173/realtime`, which nothing serves.**
   - `frontend/src/hooks/useRealtime.ts` → `resolveWsUrl()` resolves the socket
     URL from (1) an explicit `wsUrl` option, else (2) `import.meta.env.VITE_WS_URL`,
     else (3) same-origin `ws://<window.location.host>/realtime`. Locally the hook
     is mounted with no `wsUrl` and `VITE_WS_URL` is unset, so it falls to the
     same-origin default → `ws://localhost:5173/realtime` (the vite dev server
     started by `e2e/playwright.config.ts` `webServer`, base `http://localhost:5173`).
   - `frontend/vite.config.ts` `server.proxy` has only `"/api/chat"` and `"/api"`
     rules. There is **no `/realtime` proxy**, so vite answers that WS upgrade
     with nothing a socket can use → the socket never OPENs. This is exactly what
     `e2e/tests/realtime.spec.ts` `installWsProbe` / `__wsOpened` would catch.

2. **There is no local WebSocket server to proxy TO.** The realtime backend is
   written for the AWS **API Gateway v2 WebSocket API** model, not a FastAPI
   `@app.websocket` endpoint:
   - `backend/app/realtime/connect.py` / `disconnect.py` are Lambda `$connect` /
     `$disconnect` handlers (`lambda_handler(event, context)`) that read
     `event["requestContext"]["connectionId"]` and write/delete rows in the
     `ws-connections` DynamoDB table via `backend/app/realtime/connections.py`.
   - `backend/app/handlers/push.py` `_get_mgmt_client()` builds a boto3
     `apigatewaymanagementapi` client pointed at `settings.aws_endpoint_url or
     settings.ws_api_endpoint` and calls `post_to_connection(ConnectionId=…,
     Data=…)` per connection id from `connections.list_connection_ids()`.
   - `backend/app/core/config.py` `ws_api_endpoint` defaults to `""` locally.
   - `backend/app/api.py` and `backend/app/chat.py` mount only HTTP routers — no
     `@app.websocket("/realtime")` anywhere in the codebase (grep confirms the
     only WebSocket references are the AWS WS-API glue and the frontend hook).
   - The AWS WS API is provisioned only by `infra/lib/realtime-stack.ts`
     (`apigwv2.WebSocketApi` + stage, Lambdas under the RIC). That stack is **not**
     part of the local Compose stack.
   - LocalStack in `docker-compose.yml` runs `localstack/localstack:3` with
     `SERVICES: s3,sqs,dynamodb,events,secretsmanager` — `apigateway` is not even
     enabled, and the init script provisions no WS API. (API Gateway v2 WebSocket
     support in LocalStack is a paid/Base-plan feature and historically flaky; it
     is not available here regardless.) So delivery via `apigatewaymanagementapi`
     to LocalStack cannot reach any browser.

3. **No local EventBridge rule routes broadcast events to the push-queue.**
   - `backend/app/db/status.py` `transition()` publishes `dataset.status.changed`
     via `backend/app/events/publisher.py` `publish_event()` to the
     `reviewlens-events` bus; `check.updated` is published similarly from the
     check handler.
   - `infra/localstack-init/01-provision.sh` creates the bus (`reviewlens-events`)
     and the `push-queue`, but creates **no `events put-rule` / `put-targets`**
     wiring the bus to the push-queue. (In AWS this rule is `BroadcastToPushQueue`
     in `infra/lib/realtime-stack.ts`.) So locally, published events currently go
     to the bus and are dropped — the push consumer's queue never receives them.
   - `docker-compose.yml` already runs a `push-consumer` service
     (`python -m app.consumer --queue push`, `PUSH_QUEUE_URL=…/push-queue`,
     `WS_CONNECTIONS_TABLE=ws-connections`), so the consumer runtime exists and
     polls — it just never gets messages, and would have nowhere to deliver them.

**Conclusion.** Fix options (a) "point `VITE_WS_URL` at a local WS endpoint" and
(b) "add a `/realtime` vite proxy" both presuppose a backend WS server that does
not exist locally. The correct fix is option (c): **add a minimal, local-only
WebSocket relay** the browser can connect to, deliver to it from the existing
push path (gated by a new local-only env var so AWS is untouched), and add the
missing EventBridge→push-queue rule to the LocalStack init so events actually
flow. Then proxy `/realtime` through vite to the relay.

> Runtime-verification caveat (do not skip): the three gaps above are read from
> code/config. During implementation they MUST be confirmed at runtime — bring
> the stack up and watch (i) the browser socket OPEN, (ii) a published event land
> on the push-queue, (iii) the consumer deliver a frame to the relay and the
> browser receive it. Do not declare any sub-step done on reading alone.

---

## Chosen approach and why it is lowest-risk

Add a **compose-only FastAPI WebSocket relay** (`app.realtime_dev`) that:
- serves `@app.websocket("/realtime")` for the browser, and
- accepts internal HTTP `POST /broadcast` from the push consumer, fanning each
  frame out to every currently-connected browser socket (in-process connection
  set — explicitly allowed for this non-production dev path per tech.md, and
  kept isolated in its own module/service).

Then make the push handler's **delivery transport pluggable** by a new
local-only setting `ws_local_relay_url` (env `WS_LOCAL_RELAY_URL`): when set
(Compose only), `push.py` HTTP-POSTs the frame to the relay's `/broadcast`
instead of calling `apigatewaymanagementapi.post_to_connection`. Default empty
→ **the AWS path is byte-for-byte unchanged** (still `postToConnection` against
`ws_api_endpoint`). Add the missing EventBridge rule to the LocalStack init so
events reach the push-queue locally, and proxy `/realtime` in vite to the relay.

Why this over (a)/(b): there is no backend WS server to proxy to, so (a)/(b)
alone cannot work. Why a relay rather than reusing `connect.py`/`disconnect.py`
+ `apigatewaymanagementapi`: those are bound to the API-Gateway event shape and
to LocalStack's (absent) WS API — reusing them locally would mean faking API
Gateway, far more surface area and risk than a ~80-line dev relay. Why gated by
env/compose and a separate service:
- **Production untouched.** `connect.py`, `disconnect.py`, `connections.py`,
  the `apigatewaymanagementapi` delivery path, `ws_api_endpoint`,
  `infra/lib/realtime-stack.ts`, and the AWS EventBridge→SQS wiring are not
  modified. The relay is never referenced by any CDK stack and never becomes a
  Lambda command; the deployed push Lambda sets no `WS_LOCAL_RELAY_URL`, so it
  takes the existing `postToConnection` branch.
- **tech.md honored.** The relay's in-process connection state is the explicit
  dev exception (clearly isolated, not the AWS path). Model/config via config
  (new setting, no literals). SSRF guard untouched (the relay URL is an
  operator-set internal Compose address, not a user-supplied URL). No raw client
  IPs stored/logged (the relay keeps opaque socket handles only).
- **#11/#12 preserved.** `e2e/support/live.ts`, `library.spec.ts`,
  `ingestion-summary.spec.ts` are not touched. The reload-equivalent fallback
  (`allowReload: !liveAi`) still works; this change only makes the real socket
  available so `E2E_REALTIME=1 realtime.spec.ts` can pass locally.

---

## Ordered implementation items

- [ ] 1. Add a local-only relay URL setting to config (no production effect).
      Add `ws_local_relay_url: str = ""` to `Settings` in
      `backend/app/core/config.py`, documented as "LOCAL/dev only: when set
      (Compose), the push handler delivers frames by HTTP POST to this relay's
      `/broadcast` instead of API Gateway `postToConnection`. Empty in AWS, which
      keeps the production `apigatewaymanagementapi` path." Do NOT alter
      `ws_api_endpoint` or any production validator.
      Files: `backend/app/core/config.py`
      Verify: `cd backend && uv run pytest tests/unit -k config -v` passes (add a
      case asserting the field defaults to `""` if a config test module exists;
      otherwise assert via `Settings().ws_local_relay_url == ""`). Also
      `cd backend && uv run mypy app/` is clean.

- [ ] 2. Create the local-only WebSocket relay FastAPI app.
      New module `backend/app/realtime_dev.py` exposing `app = FastAPI()` with:
      (a) `@app.websocket("/realtime")` that `accept()`s, registers the socket in
      an in-process `set`, and loops on `receive()` until disconnect, removing the
      socket in a `finally`; (b) `POST /broadcast` accepting the JSON frame body,
      sending it (as text JSON) to every registered socket, dropping any that
      raise; (c) `GET /healthz` returning `{"status":"ok"}`. Add a module
      docstring stating in capitals this is LOCAL/DEV ONLY, never deployed, and is
      the one place allowed to hold in-process connection state (tech.md dev
      exception). Guard send concurrency simply (await sends sequentially; the dev
      connection count is tiny). Do not import any Lambda/event shapes.
      Files: `backend/app/realtime_dev.py`
      Verify: `cd backend && uv run pytest tests/unit/test_realtime_dev.py -v`
      (new test, item 3) passes; `cd backend && uv run ruff check . && uv run mypy app/`
      clean.

- [ ] 3. Unit-test the relay with FastAPI's TestClient WebSocket support.
      New test `backend/tests/unit/test_realtime_dev.py` (mirrors module path per
      structure.md). Cover: a client connected via
      `TestClient(app).websocket_connect("/realtime")` receives a frame posted to
      `POST /broadcast`; a broadcast with no clients is a no-op 200; two clients
      both receive the frame; `/healthz` returns 200. Use
      `fastapi.testclient.TestClient` (Starlette's WS test transport); no live
      network.
      Files: `backend/tests/unit/test_realtime_dev.py`
      Verify: `cd backend && uv run pytest tests/unit/test_realtime_dev.py -v`
      passes.

- [ ] 4. Make the push handler delivery transport pluggable (AWS branch default).
      In `backend/app/handlers/push.py`, in `_broadcast()` (or a new
      `_deliver(payload)` it calls), branch on `get_settings().ws_local_relay_url`:
      when non-empty, POST the frame to `"{ws_local_relay_url}/broadcast"` using a
      short-timeout `httpx` client (httpx is already a dependency) and return;
      when empty, run the EXISTING `apigatewaymanagementapi` fan-out unchanged.
      Keep `BROADCAST_DETAIL_TYPES`, the frame shape from `_frame()`, `GoneException`
      cleanup, and the thread-pool fan-out intact for the AWS branch. The local
      branch posts the already-built `frame` dict (decode the existing `payload`
      or pass the dict through) so the browser receives the identical
      `{"type": <event>, ...detail}` shape. Reset any memoized client in
      `reset_client()` as needed. Add `reset_client()`-friendly state only.
      Files: `backend/app/handlers/push.py`
      Verify: `cd backend && uv run pytest tests/unit -k push -v` passes
      (extend/confirm existing push handler tests: the AWS-path test must still
      exercise `post_to_connection`; add a test with `ws_local_relay_url` set that
      asserts an HTTP POST to `/broadcast` is made and `post_to_connection` is NOT
      called — mock `httpx`/the mgmt client). `cd backend && uv run mypy app/` clean.

- [ ] 5. Add the `realtime-dev` relay service to Docker Compose (local only).
      In `docker-compose.yml` add a `realtime-dev` service: build from
      `./backend` `Dockerfile`, command
      `uvicorn app.realtime_dev:app --host 0.0.0.0 --port 8002 --reload`, publish
      `8002:8002`, mount `./backend/app:/var/task/app`, env `ENV: local`,
      `SERVICE_NAME: realtime-dev`, and a `/healthz` healthcheck on 8002. Then on
      the existing `push-consumer` service, add env
      `WS_LOCAL_RELAY_URL: http://realtime-dev:8002` and a
      `depends_on: realtime-dev` (condition `service_healthy`). Do not change the
      `api`, `chat`, workers, or sweeper services' behavior.
      Files: `docker-compose.yml`
      Verify: `docker compose config` parses with no error and shows the
      `realtime-dev` service and the `push-consumer` `WS_LOCAL_RELAY_URL`
      (`docker compose config | grep -A2 WS_LOCAL_RELAY_URL`). Full runtime check
      happens in item 9.

- [ ] 6. Provision the EventBridge→push-queue rule in the LocalStack init script.
      In `infra/localstack-init/01-provision.sh`, after the bus is created, add an
      `aws events put-rule` for a rule (e.g. `reviewlens-broadcast-to-push`) on bus
      `reviewlens-events` with an event pattern matching
      `{"source":["reviewlens"],"detail-type":["dataset.status.changed","check.updated"]}`,
      then `aws events put-targets` pointing it at the `push-queue` ARN
      (`arn:aws:sqs:us-east-1:000000000000:push-queue`). Mirror the two detail
      types from `infra/lib/realtime-stack.ts` `BROADCAST_DETAIL_TYPES` exactly.
      Use the same `$AWS` helper and `2>/dev/null || true` idempotence style as the
      surrounding commands; keep the `/tmp/localstack-ready` sentinel write LAST so
      dependents still wait for full provisioning.
      Files: `infra/localstack-init/01-provision.sh`
      Verify: after `make down && make up` (item 9 gotcha applies), run
      `aws --endpoint-url=http://localhost:4566 --region=us-east-1 events list-rules --event-bus-name reviewlens-events`
      and confirm the rule exists with the SQS target
      (`events list-targets-by-rule --rule reviewlens-broadcast-to-push --event-bus-name reviewlens-events`).

- [ ] 7. Add the `/realtime` WebSocket proxy to vite (local dev server only).
      In `frontend/vite.config.ts` `server.proxy`, add a `"/realtime"` rule with
      `target: "ws://localhost:8002"` (the host-published relay port from item 5)
      and `ws: true`. Place it alongside the existing `/api` rules. This affects
      only the vite dev server (local E2E); the production CloudFront/`VITE_WS_URL`
      path is unaffected because the deployed SPA build bakes `VITE_WS_URL` and
      never uses this proxy. Add a comment noting it is the local-dev equivalent of
      the deployed `VITE_WS_URL` WS endpoint and must point at the compose
      `realtime-dev` host port.
      Files: `frontend/vite.config.ts`
      Verify: `cd frontend && npm run type-check` passes and
      `cd frontend && npm run build` succeeds (proxy config is dev-only but must
      not break the typed config/build). Runtime proxy check is item 9.

- [ ] 8. Document the local realtime path and how to run the regression spec.
      Update `e2e/README.md` "Real-time regression spec" section to note that
      local `make e2e` now serves a real `/realtime` socket via the compose
      `realtime-dev` relay + vite proxy, so `E2E_REALTIME=1 npx playwright test
      realtime.spec.ts` can be run locally (previously deployed-only). Keep the
      note that the strict no-reload path is still what the spec asserts. Do not
      change `realtime.spec.ts`, `library.spec.ts`, `ingestion-summary.spec.ts`, or
      `e2e/support/live.ts` (the #12 fix) in this item.
      Files: `e2e/README.md`
      Verify: `rg -n "realtime-dev|E2E_REALTIME=1" e2e/README.md` shows the new
      guidance (doc-only item; functional verification is item 9).

- [ ] 9. Runtime end-to-end verification of the wired local socket (the real test).
      Bring the stack up and exercise the full path. **make up gotcha:** the
      LocalStack init (incl. the new rule from item 6) only re-runs on a fresh
      LocalStack; run `make down` first so the init script re-provisions, then
      `make up` and wait for `localstack` to report healthy (the compose
      healthcheck waits on `/tmp/localstack-ready`, so `docker compose ps` showing
      `realtime-dev`, `push-consumer`, `api`, workers healthy is the gate). Then:
      (a) **Socket opens locally:**
      `cd e2e && E2E_REALTIME=1 npx playwright test realtime.spec.ts` — the
      "opens a live WebSocket channel" test must pass (the `__wsOpened` probe sees
      a `ws://localhost:5173/realtime` → relay OPEN), and the "new dataset appears
      and settles live, with no reload" test must pass (a dataset seeded from a
      second context appears and leaves `processing` purely via push).
      (b) **Event flow sanity (if a test fails, use to localize the gap):** tail
      `docker compose logs -f push-consumer realtime-dev` while seeding; confirm
      the consumer receives a push-queue message and POSTs `/broadcast`, and the
      relay logs the broadcast.
      Files: none (verification only)
      Verify: both non-skip tests in `e2e/tests/realtime.spec.ts` pass under
      `E2E_REALTIME=1` against the local stack.

- [ ] 10. Regression: full E2E and the serialized #12 specs still pass locally.
      With the stack up (item 9), run the full suite WITHOUT `E2E_REALTIME` to
      confirm the reload-equivalent fallback (#11) and the #12 isolation changes
      are intact: `cd e2e && npx playwright test` (all specs, AI stubbed). Then run
      the two #12 specs explicitly and serialized to confirm no interference:
      `cd e2e && npx playwright test library.spec.ts ingestion-summary.spec.ts --workers=1`.
      Files: none (verification only)
      Verify: `make e2e` (i.e. `cd e2e && npx playwright test`) passes green, and
      the serialized #12 run passes. `realtime.spec.ts` tests SKIP cleanly when
      `E2E_REALTIME` is unset (no false-fail).

- [ ] 11. Lint/type-check gate across backend and frontend.
      Run the project's full static checks and fix anything the new code trips.
      Files: none (verification only)
      Verify: `make lint` passes (ruff, ruff format --check, mypy on `backend/app/`;
      eslint + tsc on `frontend/`). If `ruff format --check` flags new files, run
      `cd backend && uv run ruff format .` and re-stage.

---

## Production-safety checklist (confirm before marking the task complete)

- `backend/app/realtime/connect.py`, `disconnect.py`, `connections.py` — unchanged.
- `backend/app/handlers/push.py` — AWS branch (`apigatewaymanagementapi.post_to_connection`
  against `ws_api_endpoint`) still runs whenever `WS_LOCAL_RELAY_URL` is empty,
  which is the case in the deployed push Lambda.
- `infra/lib/realtime-stack.ts` and all CDK stacks — unchanged; the relay is a
  compose-only service, never a CDK resource or Lambda command.
- `config.py` `ws_api_endpoint` and all production validators — unchanged; only a
  new empty-by-default `ws_local_relay_url` added.
- Frontend: `resolveWsUrl` precedence unchanged; production still uses the baked
  `VITE_WS_URL`. The `/realtime` vite proxy affects the dev server only.
- #12 files (`e2e/support/live.ts`, `library.spec.ts`, `ingestion-summary.spec.ts`)
  — unchanged. #11 reload-equivalent fallback intact.

## Verification command summary

- `cd backend && uv run pytest tests/unit -v` (relay + push + config units)
- `make down && make up` then wait for healthy (LocalStack re-provisions the rule)
- `cd e2e && E2E_REALTIME=1 npx playwright test realtime.spec.ts` (socket + live push)
- `cd e2e && npx playwright test` (full `make e2e`, reload fallback + #12 intact)
- `cd e2e && npx playwright test library.spec.ts ingestion-summary.spec.ts --workers=1`
- `make lint`

## Known gaps / assumptions

- Relay host port is assumed `8002`; if taken, pick another free port and keep
  the vite `/realtime` target, the compose publish, and `WS_LOCAL_RELAY_URL` in
  sync (relay is reached by service name `realtime-dev:8002` from the consumer,
  and by `localhost:8002` from the host vite dev server).
- The relay delivers whatever JSON the push handler posts; it does no filtering
  (matches `push.py`'s "broadcast to all" design — the browser decides relevance).
- `test-int`/`test-scale` are not required for this task (it touches the local
  dev relay + compose, not the integration-tested queue/storage contracts), but
  if the push-handler change affects an existing integration test, run
  `make test-int` and keep it green.

---

## Addendum — what actually shipped (deviations from the original plan)

The relay + compose + EventBridge rule + vite proxy (items 1–9) shipped as
planned. Wiring a REAL local socket then exposed convergence gaps in the
dataset-library/ingestion **frontend reducers** that were previously masked
(no socket delivered frames locally). These were fixed deliberately, after
review sign-off, and are part of this change:

1. **List badge convergence (issue #10 headline).** The approved mechanism
   evolved across review: a terminal-frame `['datasets']` invalidation was tried
   but RE-BROKE the #12 search-filter isolation (the list query holds its filter
   outside the constant `['datasets']` key, so any refetch races an active
   `tracked-search`). The shipped fix is **client-side terminal `display_state`
   derivation** (`terminalDisplayState` in `realtimeReducers.ts`): a terminal
   `dataset.status.changed` frame settles the badge IN PLACE with NO list
   refetch, so there is no filter race. Drift is guarded by a STRONG pinning
   test asserting agreement with `backend/app/datasets/library.py::
   derive_display_state` across the full terminal domain. Non-terminal in-place
   patch, unknown-id invalidation, and the reducer's exact-key coupling are
   unchanged; the property suite's oracle folds the new sticky `display_state`
   field (Correctness Property 4 still asserted).
2. **Check-session convergence.** `applyCheckUpdated` refetches `['check', id]`
   on a terminal `check.updated` (the frame carries only `{state, verdict}`, and
   the terminal in-place patch stops `useCheck`'s poll before the full verdict
   detail — incl. the "already tracked → refresh" note — arrives). Clean: no
   filter on that key to clobber.
3. **Serial E2E (option A).** `playwright.config.ts` now sets `workers: 1`
   unconditionally (was CI-only). The now-working relay broadcasts to ALL tabs
   (prod-correct), so under PARALLEL workers one test's new dataset streams into
   another's observing tab and the correct unknown-id refetch defeats that
   test's `tracked-search` scoping — a shared-backend test-isolation artifact,
   not a product defect. Serial makes LOCAL match CI (already 1 worker).
   `scopeLibraryTo` / #12 assertions and `useDatasets.ts` are untouched.

Final evidence is in `verification-10.md`.
