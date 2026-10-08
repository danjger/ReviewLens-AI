import {
  test,
  expect,
  type APIRequestContext,
  type Locator,
  type Page,
} from "@playwright/test";
import { fixtureUrl, liveAi } from "../support/env";

/**
 * Dataset Library E2E (dataset-library task 7).
 *
 * Covers the design's four "Testing Strategy → E2E" scenarios and
 * Requirements 1.4, 4.1, 5.1, 5.2, 5.3, 5.6, 6.2, 6.3:
 *
 *   1. Live updates across two browser contexts: context A has the Library
 *      open; context B adds a dataset. A sees the new row appear and move
 *      through statuses WITHOUT a reload (via the push channel, or the polling
 *      fallback when the WebSocket isn't proxied locally — 10 s list / 2 s
 *      check).  (Requirements 1.4, 6.2, 6.3)
 *   2. Archive a dataset, toggle "Show archived", see it there, then Restore.
 *      (Requirements 5.1, 5.2, 5.3)
 *   3. Refresh a dataset from the row menu and watch the row go through
 *      processing.  (Requirement 4.1)
 *   4. Submit an already-tracked URL (including an archived one) in the New
 *      Dataset panel: the ORIGINAL row is highlighted (restored if it was
 *      archived) and NO new row appears.  (Requirements 5.6, 1.4)
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * Conventions (steering testing.md): data-testid selectors only; never assert
 * on text containing counts or dates; load review pages from the fixture site
 * via `fixtureUrl(...)`, never a real site.
 *
 * Independence/idempotency: each test seeds its OWN data with a unique fixture
 * URL (a per-test query string on a fixture page) through the API, and archives
 * what it created so a re-run never collides with leftovers. Seeding goes
 * through the API request context (not the UI) so a test can set up its
 * precondition deterministically without depending on the AI-driven Check
 * verdict — see `seedDataset` below.
 *
 * Environment gating (mirrors smoke.spec.ts / ingestion.spec.ts): the Library
 * E2E needs the SPA to actually reach the live backend, and some scenarios need
 * a `will_work` Check verdict that under the default stub requires a recorded
 * Review Locator response (`make record-ai`). When a precondition can't be met
 * in the current environment the test records an annotation and skips, rather
 * than weakening a real assertion.
 */

/** Generous ceiling for the polling fallback (10 s list / 2 s check + slack). */
const LIVE_TIMEOUT = 40_000;

/** How long to wait for a processing dataset to reach a terminal state. */
const PROCESS_TIMEOUT = 60_000;

/**
 * A unique fixture URL for a test run so each test owns its data. The path is a
 * real fixture page (so a Check/collection could render it); the query string
 * makes the normalized URL unique per test, so two runs never share a dataset.
 */
function uniqueFixtureUrl(tag: string): string {
  const nonce = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  return fixtureUrl(`/extraction/plain_list/?e2e=${tag}-${nonce}`);
}

/**
 * A dataset-library row located by its id via the `data-dataset-id` attribute
 * directly on the `<tr>` (DatasetRow sets it on the row element itself).
 */
function row(page: Page, id: string): Locator {
  return page.locator(`[data-testid="dataset-row"][data-dataset-id="${id}"]`);
}

// ───────────────────────────────────────────────────────────────────────────
// Seeding + environment capability detection (through the API request context)
// ───────────────────────────────────────────────────────────────────────────

/**
 * Seed a tracked dataset directly through the Library/ingestion API so a test
 * has a deterministic precondition without relying on the AI Check verdict.
 *
 * Returns the created dataset id, or null when the backend can't be driven in
 * this environment (so the caller can annotate + skip rather than false-fail).
 *
 * Implementation note: there is no "create dataset" endpoint independent of the
 * Check flow — datasets are created by `POST /ingest/checks` + `.../add`
 * (dataset-ingestion). This seeds via that flow using a fixture URL. If the
 * check flow is unavailable (e.g. a required recorded AI verdict is missing, or
 * the stack rejects the request), it returns null.
 */
async function seedDataset(
  request: APIRequestContext,
  fixture: string,
): Promise<string | null> {
  // 1) Create a Check for the fixture URL.
  const create = await request.post("/api/ingest/checks", {
    data: { urls: [fixture] },
  });
  if (!create.ok()) return null;
  const created = (await create.json()) as {
    check_id: string;
    items: Array<{ item_id: string; state: string }>;
  };
  const checkId = created.check_id;
  const itemId = created.items[0]?.item_id;
  if (!itemId) return null;

  // 2) Poll the Check until the item reaches a terminal state with a verdict.
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

  // Only an addable verdict yields a tracked dataset. Under the default stub a
  // `will_work`/`limited` verdict needs a recorded Review Locator response, so
  // this is where a stubbed environment without that recording bails out.
  if (verdict !== "will_work" && verdict !== "limited") return null;

  // 3) Add the item so it becomes a tracked dataset.
  const add = await request.post(`/api/ingest/checks/${checkId}/add`, {
    data: { items: [{ item_id: itemId, confirm_limited: verdict === "limited" }] },
  });
  if (!add.ok()) return null;
  const addBody = (await add.json()) as {
    results: Array<{ outcome: string; dataset_id: string | null }>;
  };
  const result = addBody.results[0];
  if (!result || !result.dataset_id) return null;
  return result.dataset_id;
}

/** Best-effort cleanup: archive a dataset so a re-run starts clean. */
async function archiveViaApi(
  request: APIRequestContext,
  id: string,
): Promise<void> {
  await request.post(`/api/datasets/${id}/archive`).catch(() => undefined);
}

/**
 * Probe whether the SPA can reach the live backend in this environment. The
 * list endpoint is DB-backed and unauthenticated, so a 2xx here means the
 * browser path (vite proxy → api) is wired up. A non-2xx (e.g. the origin
 * guard rejecting a browser request that carries no CloudFront header) means
 * the Library can't load its data, so there's nothing for these tests to
 * assert — annotate + skip, like smoke.spec.ts defers the empty-library check.
 */
async function backendReachableFromSpa(page: Page): Promise<boolean> {
  const result = await page
    .evaluate(async () => {
      try {
        const response = await fetch("/api/datasets?archived=false");
        return response.status;
      } catch {
        return 0;
      }
    })
    .catch(() => 0);
  return result >= 200 && result < 300;
}

/** Annotate + skip when the SPA can't reach the backend in this environment. */
function skipBackendUnreachable(): void {
  test.info().annotations.push({
    type: "deferred",
    description:
      "The SPA could not reach the live backend (GET /api/datasets did not " +
      "return 2xx from the browser). The Library needs its list data to render, " +
      "so these tests can't assert against it in this environment. See the " +
      "report for the environment gaps (origin-verify header / rate-limit table).",
  });
  test.skip(true, "SPA cannot reach the live backend in this environment.");
}

/** Annotate + skip when a dataset couldn't be seeded (no addable verdict). */
function skipNoSeed(): void {
  if (!liveAi) {
    test.info().annotations.push({
      type: "needs-record-ai",
      description:
        "Seeding a tracked dataset needs a will_work/limited Check verdict for " +
        "the fixture page, which under the default stub requires a recorded " +
        "Review Locator response. Run `make record-ai` (ANTHROPIC_API_KEY) or " +
        "set E2E_LIVE_AI=1.",
    });
  } else {
    test.info().annotations.push({
      type: "deferred",
      description:
        "Could not seed a tracked dataset through the Check/Add flow even with " +
        "live AI; see the report for the environment gaps.",
    });
  }
  test.skip(true, "Could not seed a tracked dataset in this environment.");
}

test.describe("dataset library", () => {
  // ─────────────────────────────────────────────────────────────────────────
  // Scenario 1 — live updates across two browser contexts (no reload)
  // ─────────────────────────────────────────────────────────────────────────
  test("a new dataset appears live in another tab and moves through statuses", async ({
    page,
    browser,
    request,
  }) => {
    // Context A: the observing Library tab.
    await page.goto("/");
    await expect(page.getByTestId("app-shell")).toBeVisible({
      // Vite's first-request compile on a cold dev server can exceed the
      // default 10s expect timeout; give the initial shell render room.
      timeout: 45_000,
    });
    if (!(await backendReachableFromSpa(page))) {
      skipBackendUnreachable();
      return;
    }

    // Context B adds a dataset. Seed it through B's API request context so the
    // "add" is deterministic; A must observe the result without reloading.
    // Seed BEFORE asserting on the tracked section: on a fresh/empty library the
    // Tracked section is replaced by the empty state (design: empty points to the
    // panel), so there is nothing to observe until a dataset exists. If seeding
    // can't produce an addable verdict in this environment, skip like the others.
    const contextB = await browser.newContext();
    const requestB = contextB.request;
    const fixture = uniqueFixtureUrl("live");
    let datasetId: string | null = null;
    try {
      datasetId = await seedDataset(requestB, fixture);
    } finally {
      await contextB.close();
    }
    if (!datasetId) {
      skipNoSeed();
      return;
    }

    // With at least one tracked dataset, the Tracked section renders.
    await expect(page.getByTestId("tracked-section")).toBeVisible({
      timeout: LIVE_TIMEOUT,
    });

    // A sees the new row APPEAR without any navigation/reload. The push channel
    // delivers `dataset.status.changed`; if the WS isn't proxied locally the
    // 10 s list poll fills in. Poll generously (don't sleep-then-assert).
    const newRow = row(page, datasetId);
    await expect(newRow).toBeVisible({ timeout: LIVE_TIMEOUT });

    // And it moves to a terminal badge state WITHOUT a reload. We assert via the
    // status-badge data-state (never on text containing counts/dates). A newly
    // added dataset starts `processing` and ends `ready` (or `failed`); assert
    // it reaches a settled state the live channel delivered in place.
    const badge = newRow.getByTestId("status-badge");
    await expect(badge).toBeVisible({ timeout: LIVE_TIMEOUT });
    await expect
      .poll(async () => badge.getAttribute("data-state"), {
        timeout: PROCESS_TIMEOUT,
      })
      .not.toBe("processing");

    await archiveViaApi(request, datasetId);
  });

  // ─────────────────────────────────────────────────────────────────────────
  // Scenario 2 — archive, Show archived, restore
  // ─────────────────────────────────────────────────────────────────────────
  test("archive a dataset, see it under Show archived, then restore it", async ({
    page,
    request,
  }) => {
    await page.goto("/");
    await expect(page.getByTestId("app-shell")).toBeVisible({
      // Vite's first-request compile on a cold dev server can exceed the
      // default 10s expect timeout; give the initial shell render room.
      timeout: 45_000,
    });
    if (!(await backendReachableFromSpa(page))) {
      skipBackendUnreachable();
      return;
    }

    const fixture = uniqueFixtureUrl("archive");
    const datasetId = await seedDataset(request, fixture);
    if (!datasetId) {
      skipNoSeed();
      return;
    }

    // The row is present in the default (non-archived) list.
    await page.reload();
    const theRow = row(page, datasetId);
    await expect(theRow).toBeVisible({ timeout: LIVE_TIMEOUT });

    // Archive it from the row menu, confirming the dialog (Requirement 5.1).
    await theRow.getByTestId("row-actions-toggle").click();
    await theRow.getByTestId("action-archive").click();
    await page.getByTestId("confirm-dialog-confirm").click();

    // It drops out of the default list in place (no reload).
    await expect(theRow).toBeHidden({ timeout: LIVE_TIMEOUT });

    // Toggle "Show archived" (Requirement 5.2): the archived row appears, now
    // offering Restore.
    await page.getByTestId("tracked-show-archived").check();
    const archivedRow = row(page, datasetId);
    await expect(archivedRow).toBeVisible({ timeout: LIVE_TIMEOUT });

    // Restore it (Requirement 5.3).
    await archivedRow.getByTestId("row-actions-toggle").click();
    await archivedRow.getByTestId("action-restore").click();

    // Back in the default list: untoggle archived and confirm it's there again.
    await page.getByTestId("tracked-show-archived").uncheck();
    await expect(row(page, datasetId)).toBeVisible({ timeout: LIVE_TIMEOUT });

    await archiveViaApi(request, datasetId);
  });

  // ─────────────────────────────────────────────────────────────────────────
  // Scenario 3 — refresh from the row menu, watch processing
  // ─────────────────────────────────────────────────────────────────────────
  test("refresh a dataset from the row menu and watch it go through processing", async ({
    page,
    request,
  }) => {
    await page.goto("/");
    await expect(page.getByTestId("app-shell")).toBeVisible({
      // Vite's first-request compile on a cold dev server can exceed the
      // default 10s expect timeout; give the initial shell render room.
      timeout: 45_000,
    });
    if (!(await backendReachableFromSpa(page))) {
      skipBackendUnreachable();
      return;
    }

    const fixture = uniqueFixtureUrl("refresh");
    const datasetId = await seedDataset(request, fixture);
    if (!datasetId) {
      skipNoSeed();
      return;
    }

    await page.reload();
    const theRow = row(page, datasetId);
    await expect(theRow).toBeVisible({ timeout: LIVE_TIMEOUT });

    // Wait for it to settle to a non-processing state so Refresh is enabled
    // (Requirement 4.4 disables Refresh while in flight).
    const badge = theRow.getByTestId("status-badge");
    await expect
      .poll(async () => badge.getAttribute("data-state"), {
        timeout: PROCESS_TIMEOUT,
      })
      .not.toBe("processing");

    // Refresh from the row menu (Requirement 4.1). This starts a refresh Check
    // of the original URL; the row shows "Checking page…" then goes through
    // processing as a new version is captured.
    await theRow.getByTestId("row-actions-toggle").click();
    await theRow.getByTestId("action-refresh").click();

    // The row reflects the refresh in place: either the refresh Check badge
    // ("Checking page…", data-state="checking"), the "Ready · refreshing"
    // state, or a plain `processing`/`ready_refreshing` while the new version
    // runs. Assert the badge leaves the pre-refresh steady "ready" state.
    await expect
      .poll(async () => badge.getAttribute("data-state"), {
        timeout: LIVE_TIMEOUT,
      })
      .not.toBe("ready");

    // And eventually settles again (the refresh completes or fails, keeping the
    // previous version — Requirement 4.6). It must not get stuck "checking".
    await expect
      .poll(async () => badge.getAttribute("data-state"), {
        timeout: PROCESS_TIMEOUT,
      })
      .not.toBe("checking");

    await archiveViaApi(request, datasetId);
  });

  // ─────────────────────────────────────────────────────────────────────────
  // Scenario 4 — re-submitting a tracked (and archived) URL refreshes, never
  // duplicates; the original row is highlighted and restored.
  // ─────────────────────────────────────────────────────────────────────────
  test("re-submitting a tracked (archived) URL refreshes the original, no new row", async ({
    page,
    request,
  }) => {
    await page.goto("/");
    await expect(page.getByTestId("app-shell")).toBeVisible({
      // Vite's first-request compile on a cold dev server can exceed the
      // default 10s expect timeout; give the initial shell render room.
      timeout: 45_000,
    });
    if (!(await backendReachableFromSpa(page))) {
      skipBackendUnreachable();
      return;
    }

    // Seed a dataset, then archive it so re-submitting must RESTORE it
    // (Requirement 5.6) rather than create a second row.
    const fixture = uniqueFixtureUrl("dup");
    const datasetId = await seedDataset(request, fixture);
    if (!datasetId) {
      skipNoSeed();
      return;
    }
    await archiveViaApi(request, datasetId);

    await page.reload();

    // Count the tracked rows before re-submitting, so we can prove no NEW row
    // appears (count comparison is on the row set, not on any count/date text).
    const rowsBefore = await page.getByTestId("dataset-row").count();

    // Re-submit the SAME URL (with a tracking param appended, which the backend
    // normalizes away) in the New Dataset panel.
    const panel = page.getByTestId("new-dataset-panel");
    await expect(panel).toBeVisible();
    // Expand the panel if it's collapsed.
    if ((await panel.getAttribute("data-expanded")) !== "true") {
      await page.getByTestId("panel-toggle").click();
    }
    await page.getByTestId("tab-url").click();
    const sep = fixture.includes("?") ? "&" : "?";
    await page
      .getByTestId("url-input-textarea")
      .fill(`${fixture}${sep}utm_source=e2e`);
    await page.getByTestId("check-button").click();

    // The verdict card shows the "Already tracked → refresh" note with a link
    // to the existing dataset — proof it matched the original, not a new URL.
    const card = page.getByTestId("verdict-card");
    const trackedNote = card.getByTestId("tracked-note");
    await expect(trackedNote).toBeVisible({ timeout: PROCESS_TIMEOUT });
    await expect(trackedNote).toHaveAttribute("data-variant", "refresh");

    // Add it: the original is restored + refreshed; no new dataset is created.
    await expect(card.getByTestId("include-checkbox")).toBeChecked();
    await page.getByTestId("add-selected-button").click();
    const confirm = page.getByTestId("add-confirm-dialog");
    if ((await confirm.count()) > 0) {
      await page.getByTestId("add-confirm-confirm").click();
    }

    // The original row is back in the default list (restored, Requirement 5.6)
    // and briefly HIGHLIGHTED (Requirement 1.4). Catch the highlight via the
    // row's data-highlighted attribute (it clears after ~3 s, so poll).
    const originalRow = row(page, datasetId);
    await expect(originalRow).toBeVisible({ timeout: LIVE_TIMEOUT });
    await expect
      .poll(async () => originalRow.getAttribute("data-highlighted"), {
        timeout: LIVE_TIMEOUT,
      })
      .toBe("true");

    // No NEW row appeared: exactly one row carries this dataset id, and the
    // total row count did not grow by an extra dataset (the restored original
    // may add one row back if it had been hidden while archived, but there is
    // never a SECOND row for the same URL).
    await expect(page.locator(`[data-dataset-id="${datasetId}"]`)).toHaveCount(1);
    const rowsAfter = await page.getByTestId("dataset-row").count();
    // Re-adding a tracked URL creates no new dataset; the only possible change
    // is the restored original reappearing. So the count never grows beyond
    // "before + 1" (the restored row), and never adds a duplicate.
    expect(rowsAfter).toBeLessThanOrEqual(rowsBefore + 1);

    await archiveViaApi(request, datasetId);
  });
});
