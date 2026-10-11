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
import { expect, type Page } from "@playwright/test";

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
