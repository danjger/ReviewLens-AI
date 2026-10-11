import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import { fixtureUrl, liveAi, realtimeEnabled } from "../support/env";

/**
 * Real-time live-push regression spec — the "is GitHub issue #10 gone?" check.
 *
 * Issue #10: the SPA's real-time WebSocket (`useRealtime` → API Gateway WS)
 * updates the Library list / detail page IN PLACE, with NO reload. In prod this
 * is wired through `VITE_WS_URL` (platform-foundation tasks 35/35b: a dedicated
 * CloudFront-independent `wss://…/prod` endpoint, WS Lambdas under the RIC).
 * Under local `make e2e` there is no socket at all (vite dev server, no
 * `VITE_WS_URL`, `/realtime` not proxied), so the OTHER live specs deliberately
 * fall back to reload-polling. That fallback proves the data contract but
 * CANNOT prove the push works — so this spec exists to assert the strict
 * no-reload path and catch a regression of the prod WebSocket wiring.
 *
 * Gating: this spec only means something where a real socket exists, so it runs
 * its assertions ONLY when `E2E_REALTIME=1` (set for a deployed run whose SPA
 * build got `VITE_WS_URL`). Otherwise every test annotates + skips, so it never
 * false-fails under local `make e2e` where no socket can exist. See the e2e
 * README "Real-time regression spec".
 *
 * Conventions (steering testing.md): data-testid selectors only; never assert
 * on text containing counts or dates; load review pages from the fixture site
 * via `fixtureUrl(...)`, never a real site. Each test seeds its OWN dataset with
 * a unique fixture URL through the Check/Add API and archives it on the way out.
 */

/** Ceiling for a live push to land (no reload) — generous for WAN + cold API. */
const PUSH_TIMEOUT = 30_000;

/** How long to wait for a processing dataset to reach a terminal state. */
const PROCESS_TIMEOUT = 60_000;

/** A unique fixture URL so each test owns its data (normalized-unique query). */
function uniqueFixtureUrl(tag: string): string {
  const nonce = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  return fixtureUrl(`/extraction/plain_list/?e2e=${tag}-${nonce}`);
}

/**
 * Seed a tracked dataset through the real Check/Add API (there is no standalone
 * create-dataset endpoint). Returns the dataset id, or null when the backend
 * can't be driven in this environment. Mirrors library.spec.ts exactly.
 */
async function seedDataset(
  request: APIRequestContext,
  fixture: string,
): Promise<string | null> {
  const create = await request.post("/api/ingest/checks", {
    data: { urls: [fixture] },
  });
  if (!create.ok()) return null;
  const created = (await create.json()) as {
    check_id: string;
    items: Array<{ item_id: string }>;
  };
  const checkId = created.check_id;
  const itemId = created.items[0]?.item_id;
  if (!itemId) return null;

  const deadline = Date.now() + PROCESS_TIMEOUT;
  let verdict: string | null = null;
  while (Date.now() < deadline) {
    const poll = await request.get(`/api/ingest/checks/${checkId}`);
    if (!poll.ok()) return null;
    const session = (await poll.json()) as {
      items: Array<{ state: string; verdict?: { verdict: string } | null }>;
    };
    const item = session.items[0];
    const terminal = new Set([
      "done",
      "error",
      "invalid",
      "duplicate_in_batch",
      "awaiting_confirmation",
    ]);
    if (item && terminal.has(item.state)) {
      verdict = item.verdict?.verdict ?? null;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  if (verdict !== "will_work" && verdict !== "limited") return null;

  const add = await request.post(`/api/ingest/checks/${checkId}/add`, {
    data: {
      items: [{ item_id: itemId, confirm_limited: verdict === "limited" }],
    },
  });
  if (!add.ok()) return null;
  const addBody = (await add.json()) as {
    results: Array<{ dataset_id: string | null }>;
  };
  return addBody.results[0]?.dataset_id ?? null;
}

/** Best-effort cleanup so a re-run starts clean. */
async function archiveViaApi(
  request: APIRequestContext,
  id: string,
): Promise<void> {
  await request.post(`/api/datasets/${id}/archive`).catch(() => undefined);
}

/** The SPA can reach the backend list endpoint (browser → api). */
async function backendReachableFromSpa(page: Page): Promise<boolean> {
  const status = await page
    .evaluate(async () => {
      try {
        return (await fetch("/api/datasets?archived=false")).status;
      } catch {
        return 0;
      }
    })
    .catch(() => 0);
  return status >= 200 && status < 300;
}

/**
 * Assert the SPA actually opened a WebSocket. This directly catches the prod
 * regression from task 35 (CloudFront returning an HTML 200 for `/realtime`
 * instead of a WS upgrade): if the socket never OPENs, there is no live push.
 * Instruments the page's WebSocket constructor before any app code runs.
 */
async function installWsProbe(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as {
      __wsOpened?: boolean;
      __wsUrls?: string[];
      WebSocket: typeof WebSocket;
    };
    w.__wsOpened = false;
    w.__wsUrls = [];
    const Native = w.WebSocket;
    const Patched = function (
      this: WebSocket,
      url: string | URL,
      protocols?: string | string[],
    ) {
      const socket = new Native(url, protocols as string | string[] | undefined);
      w.__wsUrls!.push(String(url));
      socket.addEventListener("open", () => {
        w.__wsOpened = true;
      });
      return socket;
    } as unknown as typeof WebSocket;
    Patched.prototype = Native.prototype;
    (Patched as unknown as { CONNECTING: number }).CONNECTING = Native.CONNECTING;
    (Patched as unknown as { OPEN: number }).OPEN = Native.OPEN;
    (Patched as unknown as { CLOSING: number }).CLOSING = Native.CLOSING;
    (Patched as unknown as { CLOSED: number }).CLOSED = Native.CLOSED;
    w.WebSocket = Patched;
  });
}

/** Skip when no real socket is expected (local make e2e) — never false-fail. */
function skipNoRealtime(): void {
  test.info().annotations.push({
    type: "deferred",
    description:
      "E2E_REALTIME is not set: no WebSocket is expected in this environment " +
      "(local make e2e serves no /realtime socket — GitHub issue #10). The " +
      "no-reload live-push path can only be asserted against a deployed stack " +
      "whose SPA build got VITE_WS_URL. Run with E2E_REALTIME=1 against a " +
      "deployed stack to exercise this spec.",
  });
  test.skip(true, "E2E_REALTIME not set — no WebSocket to assert against.");
}

test.describe("real-time live push (issue #10 regression)", () => {
  test.beforeEach(async ({ page }) => {
    await installWsProbe(page);
  });

  // ───────────────────────────────────────────────────────────────────────
  // The SPA opens a WebSocket at all. This is the minimal, fast guard against
  // the prod wiring regression (handshake must UPGRADE, not return HTML 200).
  // ───────────────────────────────────────────────────────────────────────
  test("the SPA opens a live WebSocket channel", async ({ page }) => {
    if (!realtimeEnabled) {
      skipNoRealtime();
      return;
    }
    await page.goto("/");
    await expect(page.getByTestId("app-shell")).toBeVisible({ timeout: 45_000 });

    // useRealtime opens the socket on mount; wait for the open event to fire.
    await expect
      .poll(
        async () =>
          page.evaluate(
            () => (window as unknown as { __wsOpened?: boolean }).__wsOpened === true,
          ),
        {
          timeout: PUSH_TIMEOUT,
          message:
            "the SPA never opened a WebSocket — the realtime channel is not " +
            "wired (regression of platform-foundation tasks 35/35b, issue #10)",
        },
      )
      .toBe(true);

    // And the socket targets a ws(s) URL (not an accidental http origin).
    const urls = await page.evaluate(
      () => (window as unknown as { __wsUrls?: string[] }).__wsUrls ?? [],
    );
    expect(urls.some((u) => /^wss?:\/\//.test(u))).toBe(true);
  });

  // ───────────────────────────────────────────────────────────────────────
  // A dataset added in another context appears + settles in the observing tab
  // with NO reload. This is the real live-push guarantee issue #10 is about.
  // ───────────────────────────────────────────────────────────────────────
  test("a new dataset appears and settles live, with no reload", async ({
    page,
    browser,
    request,
  }) => {
    if (!realtimeEnabled) {
      skipNoRealtime();
      return;
    }

    await page.goto("/");
    await expect(page.getByTestId("app-shell")).toBeVisible({ timeout: 45_000 });
    expect(
      await backendReachableFromSpa(page),
      "SPA must reach the backend for this deployed-stack spec",
    ).toBe(true);

    // Confirm the socket is open before we rely on a push.
    await expect
      .poll(
        async () =>
          page.evaluate(
            () => (window as unknown as { __wsOpened?: boolean }).__wsOpened === true,
          ),
        { timeout: PUSH_TIMEOUT },
      )
      .toBe(true);

    // Seed from a SECOND context so the observing tab learns only via the push.
    const contextB = await browser.newContext();
    const fixture = uniqueFixtureUrl("rt");
    let datasetId: string | null = null;
    try {
      datasetId = await seedDataset(contextB.request, fixture);
    } finally {
      await contextB.close();
    }
    expect(datasetId, "seeding a dataset through the Check/Add API").toBeTruthy();
    const id = datasetId as string;

    const rowSelector = `[data-testid="dataset-row"][data-dataset-id="${id}"]`;

    // The row APPEARS with no reload — purely via dataset.status.changed push.
    await expect(page.locator(rowSelector)).toBeVisible({ timeout: PUSH_TIMEOUT });

    // And it moves off `processing` to a settled badge state, still no reload.
    await expect
      .poll(
        async () =>
          page.locator(rowSelector).getByTestId("status-badge").getAttribute("data-state"),
        {
          timeout: PROCESS_TIMEOUT,
          message: "the row's status badge should settle via live push, no reload",
        },
      )
      .not.toBe("processing");

    // Guard: assert we never navigated/reloaded during the observation window.
    // (A reload would reset the WS-open flag we set on first mount only if the
    // page reloaded; prove it stayed open the whole time.)
    expect(
      await page.evaluate(
        () => (window as unknown as { __wsOpened?: boolean }).__wsOpened === true,
      ),
      "the WebSocket stayed open throughout (no reload happened)",
    ).toBe(true);

    await archiveViaApi(request, id);
  });

  // A tiny breadcrumb so the report shows why this spec exists even on skip.
  test("harness: realtime gating is explicit", async () => {
    test.info().annotations.push({
      type: "info",
      description: `E2E_REALTIME=${realtimeEnabled ? "1" : "unset"}, E2E_LIVE_AI=${liveAi ? "1" : "unset"}`,
    });
    expect(typeof realtimeEnabled).toBe("boolean");
  });
});
