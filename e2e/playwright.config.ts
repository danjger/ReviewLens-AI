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
 *                       Local default: http://localhost:9090 (compose `fixtures`,
 *                       allowed by SSRF_TEST_ALLOW_HOSTS=fixtures on the backend).
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
  // Keep workers modest; the shared backend has global rate limits.
  workers: process.env.CI ? 1 : undefined,
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
