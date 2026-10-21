import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright configuration for ReviewLens AI end-to-end tests.
 *
 * Per-environment configuration is driven entirely by environment variables so
 * the same tests run against a local stack (container mode / vite dev server) or
 * a deployed stack (Lambda mode via CloudFront):
 *
 * - E2E_BASE_URL        Where the app (SPA) is reachable.
 *                       Local default: http://localhost:5173 (vite dev server).
 *                       Deployed: the stack's CloudFront URL.
 * - E2E_FIXTURE_BASE_URL Where the fixture review site is served.
 *                       Local default: http://fixtures (the compose `fixtures`
 *                       service name). The CHECK fetch runs inside the backend
 *                       container, so the URL must resolve on the Docker network
 *                       and be allowed by SSRF_TEST_ALLOW_HOSTS=fixtures. The
 *                       host-published http://localhost:9090 is NOT backend-
 *                       reachable and would be SSRF-refused ("Address not
 *                       allowed"), so it must not be used for the API-driven
 *                       seed. See support/env.ts for the full rationale.
 *                       Deployed: the public fixtures CloudFront site.
 * - E2E_LIVE_AI         "1" means the backend talks to the live Claude model.
 *                       Anything else (default unset) means the backend replaces
 *                       the Claude client with FakeClaude (recorded fixtures).
 *                       This flag is a backend/compose concern; the harness only
 *                       reads it so tests can branch (e.g. relax assertions that
 *                       depend on deterministic stubbed answers). See README.
 */

const BASE_URL = process.env.E2E_BASE_URL ?? "http://localhost:5173";

export default defineConfig({
  testDir: "./tests",
  // Fail the build on test.only left in source.
  forbidOnly: !!process.env.CI,
  // Deployed stacks and cold starts can be slow; retry once in CI.
  retries: process.env.CI ? 1 : 0,
  // Run serially (one worker) everywhere, including locally. This suite drives
  // ONE shared backend/database with no per-test namespacing, and the real-time
  // channel now broadcasts every dataset's live frames to EVERY open tab (the
  // local /realtime relay mirrors the production API Gateway WS "broadcast to
  // all" — see e2e/README and issue #10). Under PARALLEL workers that is a race:
  // one test's newly-created dataset streams into another test's observing tab,
  // where the correct unknown-id live refetch (useDatasets/realtimeReducers)
  // repopulates the Library list and defeats that test's client-side
  // `tracked-search` scoping (the #12 shared-single-stack isolation problem,
  // whose real fix is a per-run stack). Serial execution is the honest
  // acknowledgment that a shared-backend, live-broadcast suite is
  // order/concurrency-sensitive by design. CI already forced `workers: 1`; this
  // makes LOCAL match CI rather than diverge from it. (The global rate limits on
  // the shared backend are a second reason to keep workers modest.)
  workers: 1,
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never" }]]
    : [["list"]],
  timeout: 60_000,
  expect: { timeout: 10_000 },
  // Start the vite dev server for local runs so Playwright owns its lifecycle
  // (the orchestrator can't hold a long-running server). vite proxies /api →
  // http://localhost:8000 (frontend/vite.config.ts), so the SPA reaches the
  // live Compose backend. Against a deployed stack E2E_BASE_URL points at
  // CloudFront and `reuseExistingServer` lets an already-running server stand
  // in; in CI we never reuse so each run gets a fresh server.
  webServer: {
    command: "npm --prefix ../frontend run dev",
    url: "http://localhost:5173",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
