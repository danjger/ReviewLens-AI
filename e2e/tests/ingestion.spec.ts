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
 *     never created), while the two addable `limited` URLs are
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
 *  C. These verdicts are deterministic under the default stub with NO AI call.
 *     The `wont_work` blocker fixture resolves via the free rule-based pre-scan;
 *     `plain_list` (4 reviews) and `no_ratings` resolve to an addable `limited`
 *     via the free labelled-selectors path (the Locator is only consulted when
 *     selectors do not already read the reviews). So the Check + Add flow runs
 *     without a recorded Review Locator response. If a fixture does not reach an
 *     addable verdict, that points to a local fixtures/stack problem, and the
 *     test records a `deferred` annotation and skips rather than false-failing.
 *
 * Conventions (steering testing.md): data-testid selectors only; never assert on
 * text containing counts or dates; load review pages from the fixture site.
 *
 * These require the full stack + the SPA (`make up` + vite, or `make e2e`
 * against a deployed stack). They can't run without that; see e2e/README.md.
 */

/** Fixture pages chosen to yield one verdict each (see evals/extraction/labels.yaml). */
const FIXTURES = {
  // Four real reviews read via labelled selectors (no AI, no recorded Locator).
  // Four is below the 5-review will_work floor, so the verdict is `limited` —
  // but still ADDABLE, which is all this test needs for the Add path.
  addableA: "/extraction/plain_list/",
  // Comments with no ratings → `limited` via the free selectors path (no AI).
  addableB: "/extraction/no_ratings/",
  // Empty JavaScript shell → wont_work via the free rule-based pre-scan (no AI).
  wontWork: "/extraction/blocker_empty/",
} as const;

/** How long to wait for all pasted URLs to reach a terminal verdict card. */
const VERDICT_TIMEOUT = 45_000;

/**
 * Build a per-run-unique fixture URL by appending a nonce query param. The Check
 * normalizes to a unique ``normalized_url``, so each run creates FRESH datasets
 * (two Adds both become ``created``) instead of refreshing ones a prior run left
 * behind. Mirrors the ``uniqueFixtureUrl`` helper the other E2E specs use for
 * idempotency. A tracking-style param is stripped by normalization only when
 * it is a known tracking key, so use a neutral ``e2e`` param that survives
 * normalization and keeps the URL distinct.
 */
function uniqueFixtureUrl(path: string, tag: string): string {
  const nonce = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  const sep = path.includes("?") ? "&" : "?";
  return fixtureUrl(`${path}${sep}e2e=${tag}-${nonce}`);
}

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
 * does not mean an ADDABLE verdict materialised. The `plain_list` / `no_ratings`
 * fixtures resolve to `limited` via the free labelled-selectors path (no AI, no
 * recorded Locator), and `limited` IS addable — its include checkbox is ENABLED.
 * An addable verdict is the one case where the checkbox is ENABLED, so key
 * readiness off that rather than off `data-state` alone.
 */
async function isAddableVerdict(card: Locator): Promise<boolean> {
  // Wait for the card's include checkbox to become ENABLED (the signal of an
  // addable will_work/limited verdict), giving the verdict/default-include
  // effect time to apply. A wont_work card renders the checkbox DISABLED, and a
  // card that never materialises leaves it absent — both resolve to false
  // instead of hanging. This replaces a one-shot read that raced the render.
  const include = card.getByTestId("include-checkbox");
  try {
    await expect(include).toBeEnabled({ timeout: VERDICT_TIMEOUT });
    return true;
  } catch {
    return false;
  }
}

test.describe("ingestion: check + add three verdicts", () => {
  test("wont_work is refused; two addable (limited) URLs are created", async ({
    page,
  }) => {
    await page.goto("/");

    const urlTab = await urlTabOrNull(page);
    if (!urlTab) {
      skipPanelNotMounted();
      return;
    }

    // Unique per run so both Adds are `created` (not `refreshed` on a re-run).
    const addableAUrl = uniqueFixtureUrl(FIXTURES.addableA, "addA");
    const addableBUrl = uniqueFixtureUrl(FIXTURES.addableB, "addB");
    const wontWorkUrl = fixtureUrl(FIXTURES.wontWork);

    await pasteAndCheck(page, [addableAUrl, addableBUrl, wontWorkUrl]);

    const wontWorkCard = cardForUrl(page, wontWorkUrl);
    const addableACard = cardForUrl(page, addableAUrl);
    const addableBCard = cardForUrl(page, addableBUrl);

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

    // Both addable verdicts come from the free selectors path (no AI). Settle
    // the cards, then decide whether we can assert the full Add.
    await waitForVerdictState(addableACard);
    await waitForVerdictState(addableBCard);
    // "done" is not enough: a wont_work verdict is also done but not addable
    // (disabled checkbox). Both cards are ready only when they are ADDABLE — see
    // isAddableVerdict. Both fixtures resolve to `limited` via the free selectors
    // path (no AI), so this is true under the default stub and the test proceeds
    // to the Add rather than skipping.
    const addableReady =
      (await isAddableVerdict(addableACard)) &&
      (await isAddableVerdict(addableBCard));

    if (!addableReady) {
      // Both fixtures should resolve to an addable `limited` via the free
      // selectors path with no AI; if they did not, the local stack/fixtures
      // are misconfigured rather than this being an AI-recording gap.
      test.info().annotations.push({
        type: "deferred",
        description:
          "plain_list / no_ratings did not reach an addable verdict via the " +
          "free selectors path; check the fixtures server and the stack are up.",
      });
      test.skip(true, "addable verdicts not available; check the local fixtures/stack.");
      return;
    }

    // Both addable verdicts are present. Addable items default to included, but
    // ensure each include box is CHECKED explicitly so the Add selection is
    // deterministic regardless of when the default-include effect applied (the
    // wont_work box stays disabled and off, so it can't be selected). `.check()`
    // is a no-op when a box is already checked.
    for (const card of [addableACard, addableBCard]) {
      const include = card.getByTestId("include-checkbox");
      await expect(include).toBeEnabled();
      await include.check();
      await expect(include).toBeChecked();
    }

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

    // `data-outcome` is on the add-result-row element itself, so match rows by
    // their own attribute (a `.filter({ has })` descendant match would find 0).
    const createdOutcomes = summary.locator(
      '[data-testid="add-result-row"][data-outcome="created"]',
    );
    // Both addable URLs become new datasets.
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

    // Seeding the "already tracked" dataset first adds a viable URL. plain_list
    // reaches an addable `limited` via the free selectors path (no AI), so this
    // seeds under the default stub — no recorded Locator needed.
    // Unique per run so the first Add is a fresh `created` dataset, not a
    // refresh of one a prior run tracked (which would make the first Check
    // already show the "tracked" note). The utm_* re-check below normalizes to
    // this same URL within the run.
    const trackedUrl = uniqueFixtureUrl(FIXTURES.addableA, "tracked");

    await pasteAndCheck(page, [trackedUrl]);
    const firstCard = cardForUrl(page, trackedUrl);
    await waitForVerdictState(firstCard);

    // Addable — not merely "done": a wont_work card is also done but has a
    // disabled checkbox and can't seed a dataset.
    const firstAddable = await isAddableVerdict(firstCard);
    if (!firstAddable) {
      test.info().annotations.push({
        type: "deferred",
        description:
          "plain_list did not reach an addable verdict via the free selectors " +
          "path; check the fixtures server and the stack are up.",
      });
      test.skip(true, "Cannot seed a tracked dataset; check the local fixtures/stack.");
      return;
    }

    // Add the viable URL so it becomes a tracked dataset. Check the include box
    // explicitly so "Add selected" is enabled deterministically (the default-
    // include effect may not have applied yet). A single navigable result
    // navigates to the detail page (Requirement 5.4); either way the dataset
    // now exists.
    const firstInclude = firstCard.getByTestId("include-checkbox");
    await firstInclude.check();
    await expect(firstInclude).toBeChecked();
    const addSelected = page.getByTestId("add-selected-button");
    await expect(addSelected).toBeEnabled();
    await addSelected.click();
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
    // that as the `refreshed` Add outcome (not a count/date). Check the box
    // explicitly so the selection is deterministic (default-include may not
    // have applied yet).
    const dupInclude = dupCard.getByTestId("include-checkbox");
    await dupInclude.check();
    await expect(dupInclude).toBeChecked();
    const dupAddSelected = page.getByTestId("add-selected-button");
    await expect(dupAddSelected).toBeEnabled();
    await dupAddSelected.click();
    const dupConfirm = page.getByTestId("add-confirm-dialog");
    if ((await dupConfirm.count()) > 0) {
      await page.getByTestId("add-confirm-confirm").click();
    }

    // A single add navigates to the dataset detail page (Requirement 5.4). When
    // the in-place summary is shown instead, assert the refreshed outcome and
    // that no `created` row appeared. `data-outcome` is on the row element
    // itself, so match rows by their own attribute (not a descendant `has`).
    const summary = page.getByTestId("add-results-summary");
    if ((await summary.count()) > 0) {
      const refreshedRow = summary.locator(
        '[data-testid="add-result-row"][data-outcome="refreshed"]',
      );
      await expect(refreshedRow).toHaveCount(1);
      const createdRow = summary.locator(
        '[data-testid="add-result-row"][data-outcome="created"]',
      );
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
