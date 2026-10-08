/**
 * Shared environment configuration for the E2E harness.
 *
 * Centralizes how per-environment settings are read so tests don't scatter
 * `process.env` lookups. The values here mirror the documentation in
 * `playwright.config.ts` and the e2e README.
 */

/** The app (SPA) base URL. Local default is the vite dev server. */
export const baseUrl = process.env.E2E_BASE_URL ?? "http://localhost:5173";

/**
 * The fixture review site base URL.
 *
 * Local default is the compose `fixtures` container, addressed by its Docker
 * service name `http://fixtures`. The CHECK runs in the backend container,
 * which fetches this URL from inside the Docker network: there `fixtures`
 * resolves to the fixtures container and is allowed by SSRF_TEST_ALLOW_HOSTS=
 * fixtures, whereas `localhost:9090` (the host-published port) is unreachable
 * and would be SSRF-refused. The browser never fetches this URL directly (it
 * drives the SPA + its /api proxy), so the backend-reachable host is correct.
 * Deployed stacks override E2E_FIXTURE_BASE_URL with the public fixtures
 * CloudFront site.
 */
export const fixtureBaseUrl =
  process.env.E2E_FIXTURE_BASE_URL ?? "http://fixtures";

/**
 * Whether the backend is talking to the live AI model.
 *
 * The default (unset or anything other than "1") means the backend uses
 * FakeClaude recorded fixtures. Tests can use this to branch between
 * deterministic (stubbed) and best-effort (live) assertions. Forcing the
 * backend into stub mode is a backend/compose concern, not something the
 * harness controls — see the e2e README.
 */
export const liveAi = process.env.E2E_LIVE_AI === "1";

/** Build a fixture page URL from a relative path. */
export function fixtureUrl(path: string): string {
  const base = fixtureBaseUrl.replace(/\/$/, "");
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${base}${suffix}`;
}
