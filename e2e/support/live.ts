/**
 * Live-update assertions that tolerate the absence of a local WebSocket.
 *
 * In production the Portal updates the Library list and detail page IN PLACE
 * via the real-time WebSocket channel (`useRealtime` → API Gateway WS). Against
 * a deployed stack those updates arrive with no reload, and the specs assert
 * exactly that. Under local `make e2e` the SPA is the vite dev server, which
 * serves no `/realtime` WebSocket and sets no `VITE_WS_URL`, so no live frames
 * reach the browser (see GitHub issue #10 / platform-foundation Known Issues).
 * The backend STATE is still correct — only the push is missing — so locally we
 * reach the same end state by reloading between polls.
 *
 * `settleViaReload` polls a value, reloading the page between attempts, until a
 * predicate holds or the deadline passes. It asserts the same OUTCOME the live
 * assertion does (e.g. "the badge left `processing`"), just reached via a
 * reload rather than a push — so the data contract is still verified while the
 * strict no-reload live-push path remains a deployed-stack concern.
 *
 * When `E2E_LIVE_AI`-style deployed runs want the strict no-reload behavior,
 * pass `allowReload: false` to assert purely on the live channel.
 */
import { expect, type APIRequestContext, type Page } from "@playwright/test";

export interface SettleOptions {
  /** Total time to keep polling (ms). */
  timeout?: number;
  /** Delay between reload+poll attempts (ms). */
  interval?: number;
  /**
   * Reload the page between polls to pick up backend state without a live push.
   * Defaults to true (local). Set false on a deployed stack to assert the live
   * channel delivers the change with no reload.
   */
  allowReload?: boolean;
  /** A human description used in the failure message. */
  description?: string;
}

/**
 * Poll `read()` until `done(value)` is true, reloading the page between
 * attempts (unless `allowReload` is false). Throws with a clear message on
 * timeout. Returns the final value that satisfied `done`.
 */
export async function settleViaReload<T>(
  page: Page,
  read: () => Promise<T>,
  done: (value: T) => boolean,
  options: SettleOptions = {},
): Promise<T> {
  const {
    timeout = 60_000,
    interval = 2_000,
    allowReload = true,
    description = "condition",
  } = options;

  const deadline = Date.now() + timeout;
  let last: T = await read();
  if (done(last)) return last;

  while (Date.now() < deadline) {
    if (allowReload) {
      await page.reload();
      // Let the reloaded view settle before reading again.
      await page.waitForLoadState("networkidle").catch(() => undefined);
    }
    last = await read();
    if (done(last)) return last;
    await page.waitForTimeout(interval);
  }

  throw new Error(
    `settleViaReload timed out after ${timeout}ms waiting for ${description}; ` +
      `last value was ${JSON.stringify(last)}`,
  );
}

/**
 * Convenience: wait for a locator to become visible, reloading between tries.
 * Mirrors `expect(locator).toBeVisible()` but survives a missing live push.
 */
export async function expectVisibleViaReload(
  page: Page,
  testId: string,
  options: SettleOptions = {},
): Promise<void> {
  await settleViaReload(
    page,
    async () => (await page.getByTestId(testId).count()) > 0 &&
      (await page.getByTestId(testId).first().isVisible().catch(() => false)),
    (visible) => visible === true,
    { description: `testid "${testId}" to be visible`, ...options },
  );
  await expect(page.getByTestId(testId).first()).toBeVisible();
}

// ───────────────────────────────────────────────────────────────────────────
// Per-test scoping + teardown (fix #12: shared single-stack DB isolation)
//
// All E2E specs run against ONE backend and ONE accumulating database with no
// per-test reset or namespacing, so whole-list assertions (row counts,
// "appears/leaves the list") can observe sibling rows and another test's churn.
// Each test already seeds a per-run-unique fixture URL (so datasets never
// collide), and the stored `original_url` keeps the unique `e2e=<tag>-<nonce>`
// query (URL normalization only drops tracking params). These helpers use that
// unique term to (a) scope the Library list to the current test's row via the
// server-side `tracked-search` filter (`q` → `GET /api/datasets?q=` matches
// name OR original_url), and (b) tear down everything a test created so the DB
// does not grow across tests. Scoping + cleanup make the previously flaky
// whole-list assertions deterministic without weakening any deployed-run,
// no-reload live-push assertion.
// ───────────────────────────────────────────────────────────────────────────

/**
 * Extract the unique search term from a fixture URL built by `uniqueFixtureUrl`
 * (`…?e2e=<tag>-<nonce>`). The returned `<tag>-<nonce>` string is unique per
 * test and is a substring of the dataset's stored `original_url`, so both the
 * `tracked-search` box (server-side `q` filter) and the row's `row-url` match
 * only this test's row(s). Falls back to the whole URL if no `e2e=` param is
 * present, so a caller always gets a usable (if less selective) term.
 */
export function searchTermFor(fixtureUrl: string): string {
  try {
    const parsed = new URL(fixtureUrl);
    const term = parsed.searchParams.get("e2e");
    if (term) return term;
  } catch {
    // Not an absolute URL (shouldn't happen for fixture URLs) — fall through.
  }
  const match = /[?&]e2e=([^&#]+)/.exec(fixtureUrl);
  return match ? decodeURIComponent(match[1]) : fixtureUrl;
}

/**
 * Filter the Library list to a single test's row(s) via the `tracked-search`
 * box, then wait until the rendered rows reflect the filter (every visible row
 * matches `term`, or the list is empty).
 *
 * The search is a CLIENT-SIDE control: typing into the box updates the `q`
 * prop, which TanStack Query turns into a `GET /api/datasets?q=` refetch and
 * re-renders the list — no page reload is needed or wanted (a reload would
 * clear the box). This is independent of the live-push vs. reload distinction
 * from change #11: scoping is about which rows the list shows, not about how a
 * status change arrives, so it uses Playwright's built-in retrying assertions
 * (which never reload) and leaves `settleViaReload`'s semantics untouched.
 *
 * `timeout` (if given via `options`) bounds the wait. The box is filled once;
 * `expect.poll` then retries reading the rendered rows until the server-side
 * filter has landed.
 */
export async function scopeLibraryTo(
  page: Page,
  term: string,
  options: SettleOptions = {},
): Promise<void> {
  const { timeout } = options;
  const search = page.getByTestId("tracked-search");
  await expect(search).toBeVisible({ timeout });
  await search.fill(term);

  // Wait until every rendered row matches the unique term (or the list is
  // empty / shows the "no matches" state). No reload: the refetch is driven by
  // the box we just filled.
  await expect
    .poll(
      async () => {
        const rows = page.getByTestId("dataset-row");
        const total = await rows.count();
        if (total === 0) return true; // empty / no-match state satisfies the scope
        for (let i = 0; i < total; i += 1) {
          const url = await rows
            .nth(i)
            .getByTestId("row-url")
            .textContent()
            .catch(() => null);
          if (url == null || !url.includes(term)) return false;
        }
        return true;
      },
      {
        timeout,
        message: `the Library list to scope to "${term}"`,
      },
    )
    .toBe(true);
}

/**
 * Best-effort teardown: archive every dataset id a test created so the default
 * (non-archived) Library list does not accumulate across tests. Archiving (not
 * deletion) matches the product's own cleanup (`archive_dataset` only sets
 * `archived_at`), and errors are swallowed so a half-created test still tears
 * down what it can. This generalizes the per-spec `archiveViaApi` helper into
 * one call an `afterEach` can make with the whole set of created ids.
 */
export async function archiveAllCreated(
  request: APIRequestContext,
  ids: Iterable<string>,
): Promise<void> {
  for (const id of ids) {
    await request.post(`/api/datasets/${id}/archive`).catch(() => undefined);
  }
}
