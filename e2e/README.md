# E2E tests (Playwright)

End-to-end tests for ReviewLens AI. They run against either a **local stack**
(container mode via `make up`, or the vite dev server) or a **deployed stack**
(Lambda mode via CloudFront). The browser is Chromium.

Run with `make e2e` (which runs `cd e2e && npx playwright test`) or directly:

```bash
cd e2e
npm install
npx playwright install --with-deps chromium   # one-time, installs the browser
npx playwright test
```

## Per-environment configuration

Everything is driven by environment variables, so the same tests run anywhere.

| Variable | Purpose | Local default | Deployed |
|---|---|---|---|
| `E2E_BASE_URL` | Where the app (SPA) is reachable | `http://localhost:5173` (vite dev server) | the stack's CloudFront URL |
| `E2E_FIXTURE_BASE_URL` | Where the fixture review site is served | `http://fixtures` (the compose `fixtures` **service name**, reachable + allowlisted from inside the backend container) | the public fixtures CloudFront site |
| `E2E_LIVE_AI` | `1` = backend uses the live Claude model; anything else = backend uses `FakeClaude` | unset (stubbed) | unset (stubbed) |
| `E2E_REALTIME` | `1` = a real WebSocket channel is expected, so the realtime spec ASSERTS the no-reload live push; unset = it skips (no socket locally) | unset (skips) | `1` (deployed WS) |

These are read in `support/env.ts` and in `playwright.config.ts`, and threaded
to tests from there — tests import `baseUrl`, `fixtureBaseUrl`, `fixtureUrl()`,
and `liveAi` instead of touching `process.env` directly.

### Fixture site and SSRF

The backend's SSRF guard blocks private addresses, so fixture pages cannot be
served from `localhost` to the backend. Locally the compose `fixtures` host is
allowed via `SSRF_TEST_ALLOW_HOSTS=fixtures`; deployed stacks point at the
public fixtures CloudFront site (`/fixtures-site`). Production refuses to start
with `SSRF_TEST_ALLOW_HOSTS` set. See `.kiro/specs/platform-foundation`
Requirement 8.6 and design "Testing Strategy".

**Why the local default is `http://fixtures`, not `http://localhost:9090`.**
`seedDataset` creates datasets through the real API (`POST /ingest/checks` +
`.../add`), so the fixture URL is fetched and rendered by the **backend and
workers-check containers**, not by the host browser. On the Docker network the
service name `fixtures` resolves to the fixtures container and is allowlisted by
`SSRF_TEST_ALLOW_HOSTS=fixtures`. The host-published `localhost:9090` is neither
reachable from inside those containers nor on the allowlist, so a Check for it
returns `wont_work: "Address not allowed"` and seeding yields nothing. The
browser never fetches the fixture URL directly (it drives the SPA and its
`/api` proxy), so the backend-reachable host is the right default for both the
API seed and the UI flow.

### AI stub toggle

`E2E_LIVE_AI` is a **pass-through flag**. The harness reads it so tests can
branch (e.g. assert exact stubbed answers vs. best-effort live answers), but
actually forcing the backend to use `FakeClaude` is a backend/compose concern:
the deployed/compose backend must be started in stub mode when `E2E_LIVE_AI` is
not `1`. The stub itself (`FakeClaude`) lives in `backend/tests/support/ai.py`
and replays recorded responses from `backend/tests/fixtures/ai/`.

## Tests

- **`smoke.spec.ts`** — opens the app at `E2E_BASE_URL` and asserts it loads
  without a sign-in wall (Requirement 2.1). It asserts the app shell
  (`data-testid="app-shell"`) renders and that there is no login redirect. The
  empty dataset library (`data-testid="dataset-library-empty"`) is delivered by
  the `dataset-library` spec, so for now the smoke test performs a **soft**
  check for that testid and records a `deferred` annotation; promote it to a
  hard assertion once that UI exists.
- **`main-flow.spec.ts`** — the full flow (check + add URL, process, summary,
  ask question, see answer) is **completed in the `guardrailed-chat` spec**
  (tasks 7.4 / 8). It is a skipped placeholder here to document the harness
  shape and the inputs it will consume.
- **`ingestion.spec.ts`** — the `dataset-ingestion` task-10 flows (Requirements
  3.8, 5.4, 6.4): paste three fixture URLs (one of each verdict), Check, Add —
  the `wont_work` URL is refused and the others are created; and re-adding a
  tracked URL with a `utm_*` parameter refreshes the original instead of
  creating a duplicate. It uses `data-testid` selectors and the Add results
  summary outcomes (`created` / `refused_wont_work` / `refreshed`) and the
  "Already tracked" note — never counts or dates. Two cross-spec dependencies
  shape it:
  - the New Dataset panel **layout** and the **Library listing** are owned by
    `dataset-library`; until they're mounted in the app shell, each test records
    a `deferred` annotation and skips (same pattern as the smoke test's
    `dataset-library-empty` soft check). The Library-row assertion is likewise
    `deferred` to that spec; this spec asserts the Add outcomes and tracked note
    it owns.
  - the `plain_list`, `no_ratings` and `blocker_empty` fixtures all resolve to a
    deterministic verdict under the default stub via the extraction engine's
    free selector/structured path (`limited`, `limited`, `wont_work`
    respectively) — no recorded Review Locator response and no live AI call is
    required to seed a dataset. The remaining `needs-record-ai` gating in the
    specs only applies to assertions that genuinely depend on a `will_work`
    verdict or on deterministic stubbed answer text; dataset seeding itself works
    under the stub once the fixture host is backend-reachable (`http://fixtures`).

- **`realtime.spec.ts`** — the **issue #10 regression** check. It asserts the
  real-time WebSocket actually connects and that a dataset added in another
  browser context appears and settles in the observing tab **with no reload**
  (the live-push guarantee; platform-foundation tasks 35/35b). Because a real
  socket only exists against a deployed stack whose SPA build got
  `VITE_WS_URL`, the spec runs its assertions ONLY when `E2E_REALTIME=1` and
  otherwise annotates + skips — so it never false-fails under local
  `make e2e`, where there is no socket (issue #10). The OTHER live specs
  (`library`, `ingestion-summary`) assert the same OUTCOMES locally via a
  reload fallback (`support/live.ts`); this spec is the one that proves the
  push itself. Run it against a deployed stack:
  ```bash
  E2E_BASE_URL=https://<cloudfront-domain> E2E_REALTIME=1 npx playwright test realtime.spec.ts
  ```

## Conventions (from steering `testing.md`)

- Use `data-testid` selectors.
- Never depend on text that includes counts or dates.
- Load review pages from the fixture site, never from real websites.
