import { test, expect, type Locator, type Page } from "@playwright/test";
import { fixtureUrl, liveAi } from "../support/env";

/**
 * Ingestion E2E (dataset-ingestion task 10), against the public fixture site.
 *
 * Two flows from the design's "Testing Strategy → E2E" bullet and Requirements
 * 3.8, 5.4, 6.4:
 *
 *  1. Paste three fixture URLs (one of each verdict), Check, then Add selected:
 *     the `wont_work` URL is refused (its include checkbox is disabled and it is
 *     never created), while the `will_work` and (confirmed) `limited` URLs are
 *     created and appear in the Library.
 *  2. Paste an existing dataset's URL with a tracking parameter: no new dataset
 *     row is created and the original is refreshed (goes back to `requested`) —
 *     surfaced in this spec's UI as the "Already tracked" note and an Add result
 *     with outcome `refreshed`.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * Two cross-spec dependencies shape how these assert (both handled the way
 * smoke.spec.ts / main-flow.spec.ts handle not-yet-built UI):
 *
 *  A. The New Dataset panel LAYOUT (and the Library listing) are owned by the
 *     `dataset-library` spec. This spec supplies the panel's *contents*
 *     (data-testid="url-tab" and friends), but nothing mounts them in the app
 *     shell yet. So each flow first locates the panel; if it isn't mounted, the
 *     test records a `deferred` annotation and skips, exactly like smoke.spec.ts
 *     does for `dataset-library-empty`. Promote these to always-on once
 *     dataset-library mounts the URL tab and the Library list.
 *
 *  B. The Library *row* assertion ("the others appear in the Library") reads the
 *     Library listing UI, which is dataset-library's. Until that exposes stable
 *     testids, this spec asserts the behavior its OWN UI renders — the Add
 *     results summary outcomes (`created` / `refused_wont_work` / `refreshed`)
 *     and the tracked note — and leaves a `deferred` annotation for the
 *     Library-row check.
 *
 *  C. Verdicts are deterministic only where the backend needs no AI. Under the
 *     default stub (FakeClaude replaying tests/fixtures/ai/), the `wont_work`
 *     blocker fixtures resolve via the free rule-based pre-scan with NO AI call,
 *     so they are deterministic today. The `will_work` (plain_list) and
 *     `limited` (no_ratings) verdicts require a recorded Review Locator response
 *     that isn't committed — recording it needs `make record-ai` (ANTHROPIC_API_KEY,
 *     a "needs a person" step). When `E2E_LIVE_AI` is unset and a required
 *     verdict never materialises, the test records a `needs-record-ai` annotation
 *     and skips the AI-dependent assertions rather than hanging or false-failing.
 *
 * Conventions (steering testing.md): data-testid selectors only; never assert on
 * text containing counts or dates; load review pages from the fixture site.
 *
 * These require the full stack + the SPA (`make up` + vite, or `make e2e`
 * against a deployed stack). They can't run without that; see e2e/README.md.
 */

/** Fixture pages chosen to yield one verdict each (see evals/extraction/labels.yaml). */
const FIXTURES = {
  // Four real reviews, reusable selectors → will_work (needs recorded Locator).
  willWork: "/extraction/plain_list/",
  // Comments with no ratings → limited (needs recorded Locator).
  limited: "/extraction/no_ratings/",
  // Empty JavaScript shell → wont_work via the free rule-based pre-scan (no AI).
  wontWork: "/extraction/blocker_empty/",
} as const;

/** How long to wait for all pasted URLs to reach a terminal verdict card. */
const VERDICT_TIMEOUT = 45_000;

/**
 * Locate the mounted New Dataset panel's URL tab, or null when the panel isn't
 * mounted yet (dataset-library owns the layout — dependency A above).
 */
async function urlTabOrNull(page: Page): Promise<Locator | null> {
  const urlTab = page.getByTestId("url-tab");
  if ((await urlTab.count()) === 0) return null;
  return urlTab;
}

/** Record a deferred annotation and skip: the panel layout isn't mounted yet. */
function skipPanelNotMounted(): void {
  test.info().annotations.push({
    type: "deferred",
    description:
      'The New Dataset panel layout (host of data-testid="url-tab") is mounted ' +
      "by the dataset-library spec. Promote this test to always-on once that " +
      "panel is mounted in the app shell.",
  });
  test.skip(true, "New Dataset panel not mounted yet (dataset-library spec).");
}

/** Paste newline-joined URLs into the URL tab and click Check. */
async function pasteAndCheck(page: Page, urls: string[]): Promise<void> {
  await page.getByTestId("url-input-textarea").fill(urls.join("\n"));
  await page.getByTestId("check-button").click();
}

/** The verdict card for a given input URL (cards key off the input text). */
function cardForUrl(page: Page, inputUrl: string): Locator {
  return page
    .getByTestId("verdict-card")
    .filter({ has: page.getByTestId("card-url").filter({ hasText: inputUrl }) });
}

/**
 * Wait until a card settles into a verdict (its include checkbox renders), or
 * return the final data-state when it never gets a verdict. Used to decide,
 * without a recorded AI response, whether to assert or defer an AI verdict.
 */
async function waitForVerdictState(card: Locator): Promise<string | null> {
  const include = card.getByTestId("include-checkbox");
  try {
    await expect(include).toBeVisible({ timeout: VERDICT_TIMEOUT });
  } catch {
    // No verdict materialised in time (likely a missing recorded Locator).
  }
  return card.getAttribute("data-state");
}

/**
 * Whether a settled card represents an ADDABLE (viable) verdict.
 *
 * A card reaches `data-state="done"` for ANY terminal verdict — including
 * `wont_work`, whose include checkbox is rendered but DISABLED. So "done" alone
 * does not mean an AI `will_work`/`limited` verdict materialised. Under the stub
 * without a recorded Review Locator response, the `plain_list`/`no_ratings`
 * pages resolve to `wont_work` (done, checkbox disabled), which must be treated
 * as "fixture missing → skip", not "verdict ready → assert". An addable verdict
 * is the one case where the include checkbox is ENABLED, so key readiness off
 * that rather than off `data-state` alone.
 */
async function isAddableVerdict(card: Locator): Promise<boolean> {
  const include = card.getByTestId("include-checkbox");
  if ((await include.count()) === 0) return false;
  if (!(await include.isVisible().catch(() => false))) return false;
  return include.isEnabled().catch(() => false);
}

test.describe("ingestion: check + add three verdicts", () => {
  test("wont_work is refused; will_work and limited are created", async ({
    page,
  }) => {
    await page.goto("/");

    const urlTab = await urlTabOrNull(page);
    if (!urlTab) {
      skipPanelNotMounted();
      return;
    }

    const willWorkUrl = fixtureUrl(FIXTURES.willWork);
    const limitedUrl = fixtureUrl(FIXTURES.limited);
    const wontWorkUrl = fixtureUrl(FIXTURES.wontWork);

    await pasteAndCheck(page, [willWorkUrl, limitedUrl, wontWorkUrl]);

    const wontWorkCard = cardForUrl(page, wontWorkUrl);
    const willWorkCard = cardForUrl(page, willWorkUrl);
    const limitedCard = cardForUrl(page, limitedUrl);

    // The wont_work blocker is deterministic under the stub (no AI call).
    await expect(wontWorkCard).toHaveAttribute("data-state", "done", {
      timeout: VERDICT_TIMEOUT,
    });
    // Requirement 3.8: a wont_work URL cannot be added — its include checkbox is
    // present but disabled (and off), so a stale selection can't submit it.
    const wontWorkInclude = wontWorkCard.getByTestId("include-checkbox");
    await expect(wontWorkInclude).toBeVisible();
    await expect(wontWorkInclude).toBeDisabled();
    await expect(wontWorkInclude).not.toBeChecked();

    // The will_work / limited verdicts depend on a recorded Review Locator
    // response (dependency C). Decide whether we can assert the full Add.
    await waitForVerdictState(willWorkCard);
    await waitForVerdictState(limitedCard);
    // "done" is not enough: a wont_work verdict is also done but not addable
    // (disabled checkbox). The AI verdicts are only truly ready when both cards
    // are addable — see isAddableVerdict. Under the stub without the recorded
    // Locator, plain_list/no_ratings resolve to wont_work, so this is false and
    // the test skips (needs `make record-ai`) instead of asserting a disabled
    // checkbox is checked.
    const aiVerdictsReady =
      (await isAddableVerdict(willWorkCard)) &&
      (await isAddableVerdict(limitedCard));

    if (!aiVerdictsReady && !liveAi) {
      test.info().annotations.push({
        type: "needs-record-ai",
        description:
          "will_work (plain_list) and limited (no_ratings) verdicts need a " +
          "recorded Review Locator response. Run `make record-ai` " +
          "(ANTHROPIC_API_KEY) to record it, or set E2E_LIVE_AI=1. Asserted the " +
          "deterministic wont_work refusal only.",
      });
      test.skip(
        true,
        "AI-dependent verdicts unavailable under the stub; needs `make record-ai`.",
      );
      return;
    }

    // Both AI verdicts are present: include them (will_work is on by default;
    // limited is on by default too) and Add selected. The wont_work item stays
    // excluded (disabled checkbox), so the Add must refuse it.
    await expect(willWorkCard.getByTestId("include-checkbox")).toBeChecked();
    await expect(limitedCard.getByTestId("include-checkbox")).toBeChecked();

    await page.getByTestId("add-selected-button").click();

    // A limited item is selected, so the confirmation dialog appears first
    // (Requirement 3.9); confirm it to send confirm_limited for the limited URL.
    const confirmDialog = page.getByTestId("add-confirm-dialog");
    if ((await confirmDialog.count()) > 0) {
      await expect(confirmDialog).toBeVisible();
      await page.getByTestId("add-confirm-confirm").click();
    }

    // Several URLs were added, so the UI stays put and shows the results summary
    // (Requirement 5.4). Assert via outcomes this spec's UI renders (dependency B).
    const summary = page.getByTestId("add-results-summary");
    await expect(summary).toBeVisible();

    const createdOutcomes = summary
      .getByTestId("add-result-row")
      .filter({ has: page.locator('[data-outcome="created"]') });
    // Both the will_work and limited URLs become new datasets.
    await expect(createdOutcomes).toHaveCount(2);

    // The wont_work URL was not added. It is either refused in the summary or
    // simply absent (it was never part of the selection); assert it never
    // produced a `created` row.
    const wontWorkRow = summary
      .getByTestId("add-result-row")
      .filter({ hasText: wontWorkUrl });
    for (const row of await wontWorkRow.all()) {
      await expect(row).not.toHaveAttribute("data-outcome", "created");
    }

    // Deferred: "the others appear in the Library" reads the dataset-library
    // listing UI, which that spec owns (dependency B).
    const libraryRows = page.getByTestId("dataset-row");
    if ((await libraryRows.count()) > 0) {
      await expect.soft(libraryRows.first()).toBeVisible();
    } else {
      test.info().annotations.push({
        type: "deferred",
        description:
          'The Library row assertion reads data-testid="dataset-row" from the ' +
          "dataset-library listing UI. Promote to a hard assertion once that " +
          "listing exists; this spec asserts the Add outcomes it owns.",
      });
    }
  });
});

test.describe("ingestion: re-adding a tracked URL refreshes, never duplicates", () => {
  test("a tracked URL with a tracking parameter refreshes the original", async ({
    page,
  }) => {
    await page.goto("/");

    const urlTab = await urlTabOrNull(page);
    if (!urlTab) {
      skipPanelNotMounted();
      return;
    }

    // Seeding the "already tracked" dataset requires first adding a viable
    // (will_work) URL, which needs a recorded Locator response (dependency C).
    // Without it (and not live), there is nothing to re-add, so defer.
    const baseUrlPath = FIXTURES.willWork;
    const trackedUrl = fixtureUrl(baseUrlPath);

    await pasteAndCheck(page, [trackedUrl]);
    const firstCard = cardForUrl(page, trackedUrl);
    await waitForVerdictState(firstCard);

    // Addable (will_work) — not merely "done": under the stub plain_list
    // resolves to wont_work (done, disabled), which can't seed a dataset.
    const firstAddable = await isAddableVerdict(firstCard);
    if (!firstAddable && !liveAi) {
      test.info().annotations.push({
        type: "needs-record-ai",
        description:
          "Seeding a tracked dataset needs a will_work verdict for plain_list, " +
          "which requires a recorded Review Locator response (`make record-ai`, " +
          "ANTHROPIC_API_KEY) or E2E_LIVE_AI=1.",
      });
      test.skip(true, "Cannot seed a tracked dataset without a recorded verdict.");
      return;
    }

    // Add the viable URL so it becomes a tracked dataset. A single navigable
    // result navigates to the detail page (Requirement 5.4); either way the
    // dataset now exists.
    await page.getByTestId("add-selected-button").click();
    const firstConfirm = page.getByTestId("add-confirm-dialog");
    if ((await firstConfirm.count()) > 0) {
      await page.getByTestId("add-confirm-confirm").click();
    }
    await page.waitForLoadState("networkidle");

    // Start a fresh Check for the SAME URL but with a tracking parameter
    // appended. Normalization strips `utm_*` (Requirement 6.1), so this must
    // match the dataset added above rather than create a second one.
    await page.goto("/");
    const urlTabAgain = await urlTabOrNull(page);
    if (!urlTabAgain) {
      skipPanelNotMounted();
      return;
    }

    const sep = trackedUrl.includes("?") ? "&" : "?";
    const trackedWithParam = `${trackedUrl}${sep}utm_source=x`;
    await pasteAndCheck(page, [trackedWithParam]);

    const dupCard = cardForUrl(page, trackedWithParam);
    await waitForVerdictState(dupCard);

    // Requirement 6.3: the card shows the "Already tracked" note (refresh
    // variant) with a link to the existing dataset — proving no duplicate and
    // that adding will refresh.
    const trackedNote = dupCard.getByTestId("tracked-note");
    await expect(trackedNote).toBeVisible();
    await expect(trackedNote).toHaveAttribute("data-variant", "refresh");
    await expect(dupCard.getByTestId("tracked-link")).toBeVisible();

    // Add it: Requirement 6.4 — no new dataset is created; the original is
    // refreshed (its status goes back to `requested`). This spec's UI surfaces
    // that as the `refreshed` Add outcome (not a count/date).
    await expect(dupCard.getByTestId("include-checkbox")).toBeChecked();
    await page.getByTestId("add-selected-button").click();
    const dupConfirm = page.getByTestId("add-confirm-dialog");
    if ((await dupConfirm.count()) > 0) {
      await page.getByTestId("add-confirm-confirm").click();
    }

    // A single add navigates to the dataset detail page (Requirement 5.4). When
    // the in-place summary is shown instead, assert the refreshed outcome and
    // that no `created` row appeared.
    const summary = page.getByTestId("add-results-summary");
    if ((await summary.count()) > 0) {
      const refreshedRow = summary
        .getByTestId("add-result-row")
        .filter({ has: page.locator('[data-outcome="refreshed"]') });
      await expect(refreshedRow).toHaveCount(1);
      const createdRow = summary
        .getByTestId("add-result-row")
        .filter({ has: page.locator('[data-outcome="created"]') });
      await expect(createdRow).toHaveCount(0);
    } else {
      // Navigated to the detail page: the tracked note already proved the
      // refresh-not-duplicate behavior above.
      test.info().annotations.push({
        type: "info",
        description:
          "Single refresh navigated to the dataset detail page (Req 5.4); the " +
          '"Already tracked → refresh" note asserted the no-duplicate behavior.',
      });
    }

    // Deferred: confirming "no new row appears" in the Library listing needs the
    // dataset-library listing UI (dependency B).
    test.info().annotations.push({
      type: "deferred",
      description:
        'Asserting the Library has no new row (one dataset per URL) reads the ' +
        "dataset-library listing. Promote once that listing exposes stable " +
        "testids; this spec asserts the tracked note + refreshed outcome it owns.",
    });
  });
});
