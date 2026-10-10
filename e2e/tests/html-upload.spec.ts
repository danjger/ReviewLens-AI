import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { test, expect, type Locator, type Page } from "@playwright/test";
import { liveAi } from "../support/env";

/**
 * HTML-upload E2E (dataset-ingestion task 24, Requirements 8.7, 8.9, 8.14).
 *
 * The "Saved page" tab lets an analyst upload the saved HTML of a review page
 * their own browser rendered, for sites the app's own capture can't read. The
 * flow (design "HTML upload path" / "Frontend / HTML tab"):
 *
 *   Saved-page tab → dropzone (`.html`/`.htm`/`.mhtml`) → pre-signed PUT upload
 *   (`POST /uploads`) → `POST /ingest/html-checks` starts a one-item Check on
 *   the uploaded markup → a verdict card identical to the URL card (badge with
 *   text not colour, reasons, evidence, verified sample reviews copied from the
 *   uploaded page) → required name + optional source URL/description → Add.
 *
 * Two scenarios, matching the task:
 *
 *   1. Upload the large saved fixture page (fixtures-site/extraction/
 *      large_server_rendered/index.html — ≥20 server-rendered reviews, added in
 *      task 23) as HTML, confirm a `will_work` verdict card WITH sample reviews,
 *      Add it, and confirm the dataset appears (its detail page / a Library row)
 *      and processes (Requirements 8.7, 8.9, 8.14, 9.3).
 *   2. Upload a blocked/empty saved page (the existing blocker_empty shell) and
 *      confirm the `wont_work` verdict refuses it — Add stays disabled, so it is
 *      not addable (Requirement 8.9 / 3.8).
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * The AI stub + environment gating mirrors ingestion.spec.ts exactly:
 *
 *  A. The New Dataset panel LAYOUT (host of the "Saved page" tab, and the
 *     Library listing) are owned by `dataset-library`. This spec supplies the
 *     tab's CONTENTS (data-testid="html-tab" and friends). Until the panel is
 *     mounted in the app shell, each test records a `deferred` annotation and
 *     skips, exactly like smoke.spec.ts does for `dataset-library-empty`.
 *
 *  B. Verdicts are deterministic only where the backend needs no AI. Under the
 *     default stub (FakeClaude replaying tests/fixtures/ai/), the empty-shell
 *     blocker resolves via the FREE rule-based pre-scan with NO AI call, so the
 *     `wont_work` case is deterministic today. The large server-rendered page's
 *     `will_work` verdict needs a recorded Review Locator response that isn't
 *     committed — recording it needs `make record-ai` (ANTHROPIC_API_KEY, a
 *     "needs a person" step). When `E2E_LIVE_AI` is unset and the `will_work`
 *     verdict never materialises, that test records a `needs-record-ai`
 *     annotation and skips, rather than hanging or false-failing.
 *
 *  C. The Library ROW assertion ("the dataset appears in the Library") reads the
 *     dataset-library listing UI. A single HTML Add navigates to the dataset's
 *     detail page (Requirement 5.4 / 8.9), so this spec asserts the detail page
 *     renders and the dataset processes (its status badge leaves `processing`),
 *     and treats the Library-row check as a soft/deferred check it does not own.
 *
 * Conventions (steering testing.md): data-testid selectors only; never assert on
 * text containing counts or dates; load review pages from the fixture site — here
 * the SAME saved fixture page is uploaded as a file, never fetched from a real
 * site (Requirement 9.3).
 *
 * These require the full stack + the SPA (`make up` + vite, or `make e2e`
 * against a deployed stack). They can't run without that; see e2e/README.md.
 */

/** Resolve a repo-relative path from this test file's location. */
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..");

/**
 * The HTML/CSV upload uses a BROWSER-side pre-signed PUT (Requirement 8.1 /
 * 7.1): `POST /uploads` returns a pre-signed URL and the browser PUTs the file
 * straight to object storage, bypassing the API. Locally that pre-signed URL is
 * minted by LocalStack with the Docker-internal host `localstack:4566`, which
 * the browser (outside the Compose network) cannot resolve; and even published
 * on `localhost:4566`, a cross-origin browser PUT needs CORS headers LocalStack
 * doesn't send. So fulfil the direct-to-storage PUT from the Playwright request
 * context (which is not subject to CORS), relaying the bytes to the published
 * `localhost:4566` endpoint and returning its status to the browser. The app's
 * upload hook then proceeds exactly as in production.
 *
 * This is a LOCAL harness concern only: against a deployed stack the pre-signed
 * URL already points at a browser-reachable S3 endpoint with proper CORS, so
 * the `localstack` matcher never fires and the rewrite is a no-op. (The vite
 * proxy only rewrites `/api`, not these direct-to-storage PUTs.)
 */
async function relayLocalstackPut(page: Page): Promise<void> {
  // Permissive CORS so the browser accepts the fulfilled cross-origin response.
  const cors: Record<string, string> = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET,PUT,POST,OPTIONS",
    "access-control-allow-headers": "*",
  };

  await page.route("**://localstack:4566/**", async (route) => {
    const request = route.request();

    // A cross-origin PUT with a non-simple Content-Type triggers a CORS
    // preflight; answer it ourselves (LocalStack won't send CORS headers).
    if (request.method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: cors, body: "" });
      return;
    }

    const target = request.url().replace("://localstack:4566/", "://localhost:4566/");
    try {
      const response = await page.request.fetch(target, {
        method: request.method(),
        headers: request.headers(),
        data: request.postDataBuffer() ?? undefined,
      });
      await route.fulfill({
        status: response.status(),
        headers: { ...response.headers(), ...cors },
        body: await response.body(),
      });
    } catch {
      // Let the browser see the failure so the test's upload-error guard fires.
      await route.abort();
    }
  });
}

/**
 * The saved fixture pages uploaded as files.
 *
 * - The large server-rendered page (task 23) has ≥20 reviews with text, rating,
 *   date and author → a genuine `will_work` under the default thresholds
 *   (Requirement 9.2). Uploading it reuses the SAME source page the URL-check
 *   path loads over HTTP (Requirement 9.3).
 * - The empty JavaScript shell is a blocker (no readable content) → `wont_work`
 *   via the rule-based pre-scan, deterministic with no AI call.
 */
const WILL_WORK_FILE = resolve(
  REPO_ROOT,
  "fixtures-site/extraction/large_server_rendered/index.html",
);
const WONT_WORK_FILE = resolve(
  REPO_ROOT,
  "fixtures-site/extraction/blocker_empty/index.html",
);

/** How long to wait for an uploaded page to reach a terminal verdict card. */
const VERDICT_TIMEOUT = 45_000;

/** How long to wait for an added dataset to leave `processing`. */
const PROCESS_TIMEOUT = 60_000;

/**
 * Locate the mounted "Saved page" (HTML) tab button, or null when the New
 * Dataset panel isn't mounted yet (dataset-library owns the layout —
 * dependency A).
 */
async function htmlTabButtonOrNull(page: Page): Promise<Locator | null> {
  const tab = page.getByTestId("tab-html");
  if ((await tab.count()) === 0) return null;
  return tab;
}

/** Record a deferred annotation and skip: the panel layout isn't mounted yet. */
function skipPanelNotMounted(): void {
  test.info().annotations.push({
    type: "deferred",
    description:
      'The New Dataset panel layout (host of data-testid="tab-html") is mounted ' +
      "by the dataset-library spec. Promote this test to always-on once that " +
      "panel is mounted in the app shell.",
  });
  test.skip(true, "New Dataset panel not mounted yet (dataset-library spec).");
}

/**
 * Open the "Saved page" tab and upload a saved HTML file through the dropzone's
 * file input, driving the real pre-signed PUT + `POST /ingest/html-checks`.
 */
async function openHtmlTabAndUpload(page: Page, filePath: string): Promise<void> {
  // Make the browser-side pre-signed PUT reachable locally (see helper above).
  await relayLocalstackPut(page);

  // Expand the panel if dataset-library collapsed it, then switch to the tab.
  const panel = page.getByTestId("new-dataset-panel");
  if ((await panel.count()) > 0) {
    if ((await panel.getAttribute("data-expanded")) !== "true") {
      const toggle = page.getByTestId("panel-toggle");
      if ((await toggle.count()) > 0) await toggle.click();
    }
  }
  await page.getByTestId("tab-html").click();
  await expect(page.getByTestId("html-tab")).toBeVisible();

  // Setting the file input fires the dropzone's onFile → upload pipeline.
  await page.getByTestId("upload-file-input").setInputFiles(filePath);
}

/** The single verdict card the HTML tab renders once the assessment lands. */
function htmlVerdictCard(page: Page): Locator {
  return page.getByTestId("html-tab-results").getByTestId("verdict-card");
}

/**
 * Wait for the HTML tab's verdict card to settle, then report the verdict
 * badge's state label (`will_work` / `limited` / `wont_work`) or null when no
 * verdict materialised in time (e.g. a missing recorded Locator response).
 */
async function waitForHtmlVerdict(page: Page): Promise<string | null> {
  const card = htmlVerdictCard(page);
  try {
    await expect(card).toHaveAttribute("data-state", "done", {
      timeout: VERDICT_TIMEOUT,
    });
  } catch {
    return null;
  }
  // The badge conveys the verdict with text, not colour alone (Requirement
  // 8.14 / 3.7); read its machine-readable state.
  const badge = card.getByTestId("verdict-badge");
  if ((await badge.count()) === 0) return null;
  return badge.getAttribute("data-verdict");
}

test.describe("html upload: large saved page is addable (will_work)", () => {
  test("upload the large fixture page, see a will_work card with samples, add it", async ({
    page,
  }) => {
    await page.goto("/");

    const tab = await htmlTabButtonOrNull(page);
    if (!tab) {
      skipPanelNotMounted();
      return;
    }

    await openHtmlTabAndUpload(page, WILL_WORK_FILE);

    const verdict = await waitForHtmlVerdict(page);
    const card = htmlVerdictCard(page);

    // The `will_work` verdict needs a recorded Review Locator response
    // (dependency B). Under the stub without it, the large page cannot reach
    // `will_work`, so skip the addable assertions rather than false-fail.
    if (verdict !== "will_work" && !liveAi) {
      test.info().annotations.push({
        type: "needs-record-ai",
        description:
          "The large server-rendered page's will_work verdict needs a recorded " +
          "Review Locator response. Run `make record-ai` (ANTHROPIC_API_KEY) or " +
          "set E2E_LIVE_AI=1. The deterministic wont_work refusal is covered by " +
          "the other test in this spec.",
      });
      test.skip(
        true,
        "will_work verdict unavailable under the stub; needs `make record-ai`.",
      );
      return;
    }

    // The card is a genuine will_work (Requirements 8.7, 9.2).
    expect(verdict).toBe("will_work");

    // It shows verified sample reviews copied from the uploaded page
    // (Requirement 8.7): expand the samples and assert at least one non-empty
    // text appears. Never assert the COUNT (steering: no count/date text).
    const samples = card.getByTestId("sample-reviews");
    await expect(samples).toBeVisible();
    await card.getByTestId("toggle-samples").click();
    const sampleTexts = card.getByTestId("sample-text");
    await expect(sampleTexts.first()).toBeVisible();
    await expect(sampleTexts.first()).not.toBeEmpty();

    // Add is gated on a required name (Requirement 8.14): empty name → disabled.
    const addButton = page.getByTestId("html-add-button");
    await expect(addButton).toBeDisabled();

    // Fill the required name; Add becomes enabled for a will_work item.
    await page.getByTestId("html-name-input").fill("Pathfinder Standing Desk (saved page)");
    await expect(addButton).toBeEnabled();

    // Add it. A will_work item needs no confirmation (that's the limited path),
    // and a single navigable result navigates to the dataset detail page
    // (Requirement 5.4 / 8.9).
    await addButton.click();

    // Confirm the dataset was created and processes. Exactly one HTML upload
    // navigates to its detail page; assert that page renders, then that it
    // reaches an ACTIVE (processed) version — the metrics panel only renders
    // once a version is active (ingestion-summary), so its appearance is the
    // "processed" signal here (never asserting count/date text).
    await expect(page.getByTestId("dataset-detail-page")).toBeVisible({
      timeout: PROCESS_TIMEOUT,
    });
    await expect(page.getByTestId("metrics-panel")).toBeVisible({
      timeout: PROCESS_TIMEOUT,
    });

    // Deferred: "appears in the Library" reads the dataset-library listing UI
    // (dependency C). The detail-page assertion above already proves the dataset
    // was created and processed; soft-check a Library row if the listing exists.
    await page.goto("/");
    const libraryRows = page.getByTestId("dataset-row");
    if ((await libraryRows.count()) > 0) {
      await expect.soft(libraryRows.first()).toBeVisible();
    } else {
      test.info().annotations.push({
        type: "deferred",
        description:
          'The Library row assertion reads data-testid="dataset-row" from the ' +
          "dataset-library listing UI. Promote to a hard assertion once that " +
          "listing exists; this spec asserts the detail page + processing it owns.",
      });
    }
  });
});

test.describe("html upload: blocked/empty saved page is refused (wont_work)", () => {
  test("upload an empty shell, see wont_work, and Add stays disabled", async ({
    page,
  }) => {
    await page.goto("/");

    const tab = await htmlTabButtonOrNull(page);
    if (!tab) {
      skipPanelNotMounted();
      return;
    }

    await openHtmlTabAndUpload(page, WONT_WORK_FILE);

    // The empty JavaScript shell is a blocker: `wont_work` via the free
    // rule-based pre-scan, deterministic under the stub (no AI call).
    const verdict = await waitForHtmlVerdict(page);
    expect(verdict).toBe("wont_work");

    // Requirement 8.9 / 3.8: a wont_work upload cannot be added. Even with a
    // name supplied, the Add button stays disabled because the verdict is not
    // addable (`isAddable` is false for wont_work).
    await page.getByTestId("html-name-input").fill("A blocked saved page");
    const addButton = page.getByTestId("html-add-button");
    await expect(addButton).toBeDisabled();

    // And no confirmation dialog or results summary ever appears (nothing was
    // submitted). Clicking the disabled button is a no-op; assert the Add
    // surfaces stay absent.
    await addButton.click({ force: true }).catch(() => undefined);
    await expect(page.getByTestId("add-confirm-dialog")).toHaveCount(0);
    await expect(page.getByTestId("add-results-summary")).toHaveCount(0);
  });
});
