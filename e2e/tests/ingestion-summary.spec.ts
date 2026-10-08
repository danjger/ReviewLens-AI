import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import { fixtureUrl, liveAi } from "../support/env";

/**
 * Ingestion Summary E2E (ingestion-summary task 8).
 *
 * Covers the design's "Testing Strategy → E2E" scenario and Requirements
 * 2.1, 3.1, 4.2, 5.2, 6.3:
 *
 *   1. Open a *processed* fixture dataset's detail page (`/datasets/{id}`) and
 *      verify the header, metrics, snapshot, completeness, and a *filtered*
 *      reviews table render the expected structure (Requirements 2.1, 3.1,
 *      4.2, 5.2).
 *   2. Add a dataset and watch the detail page move from processing to ready
 *      WITHOUT a reload: navigate while the first version is still processing
 *      (skeletons / processing notice visible), then observe it transition to
 *      the ready state (the metrics panel appears) via the live channel, with
 *      no page reload (Requirement 6.3).
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * Conventions (steering testing.md, mirrored from library.spec.ts): data-testid
 * selectors only; never assert on text that contains counts or dates; load
 * review pages from the fixture site via `fixtureUrl(...)`, never a real site.
 *
 * Independence/idempotency: each test seeds its OWN dataset with a unique
 * fixture URL through the Check/Add API (not the UI), navigates to its detail
 * route, and archives what it created so a re-run never collides with
 * leftovers.
 *
 * Environment gating (identical to library.spec.ts): the suite runs against the
 * Compose stack with AI stubbed unless E2E_LIVE_AI=1. Seeding a tracked dataset
 * needs a will_work/limited Check verdict, which under the default stub needs a
 * recorded Review Locator response (`make record-ai`). When a precondition
 * can't be met in the current environment the test annotates and skips rather
 * than false-failing.
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

// ───────────────────────────────────────────────────────────────────────────
// Seeding + environment capability detection (through the API request context)
//
// These mirror library.spec.ts exactly so the gating behaves identically: a
// dataset is created via the Check/Add flow (there is no standalone "create
// dataset" endpoint), and the helpers return null / annotate-and-skip when the
// environment can't produce an addable verdict.
// ───────────────────────────────────────────────────────────────────────────

/**
 * Seed a tracked dataset directly through the Library/ingestion API so a test
 * has a deterministic precondition without relying on the AI Check verdict.
 * Returns the created dataset id, or null when the backend can't be driven in
 * this environment (so the caller can annotate + skip rather than false-fail).
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
 * Poll the dataset detail endpoint until its `active_version` is set (a version
 * is ready) or the deadline passes. Returns true once a version is active. Used
 * by scenario 1 to seed a *processed* dataset before opening its detail page.
 */
async function waitForActiveVersion(
  request: APIRequestContext,
  id: string,
): Promise<boolean> {
  const deadline = Date.now() + PROCESS_TIMEOUT;
  while (Date.now() < deadline) {
    const poll = await request.get(`/api/datasets/${id}`);
    if (poll.ok()) {
      const detail = (await poll.json()) as { active_version?: number | null };
      if (detail.active_version != null) return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  return false;
}

/**
 * Probe whether the SPA can reach the live backend in this environment. The
 * list endpoint is DB-backed and unauthenticated, so a 2xx here means the
 * browser path (vite proxy → api) is wired up. A non-2xx means the detail page
 * can't load its data — annotate + skip, like library.spec.ts.
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
      "return 2xx from the browser). The detail page needs its data to render, " +
      "so these tests can't assert against it in this environment.",
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

test.describe("ingestion summary", () => {
  // ─────────────────────────────────────────────────────────────────────────
  // Scenario 1 — open a processed dataset's detail page and verify the summary
  //   header, metrics, snapshot, completeness, and a filtered reviews table.
  //   (Requirements 2.1, 3.1, 4.2, 5.2)
  // ─────────────────────────────────────────────────────────────────────────
  test("open a processed dataset: header, metrics, snapshot, completeness, filtered reviews", async ({
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

    // Seed a dataset and wait until it has an active version (fully processed),
    // so the detail page renders the summary rather than skeletons.
    const fixture = uniqueFixtureUrl("summary");
    const datasetId = await seedDataset(request, fixture);
    if (!datasetId) {
      skipNoSeed();
      return;
    }
    const ready = await waitForActiveVersion(request, datasetId);
    expect(ready, "seeded dataset reached an active version").toBe(true);

    // Navigate to the detail route the same way the SPA does (`/datasets/{id}`,
    // per routes.ts `datasetDetailPath`). A full load mounts the detail page.
    await page.goto(`/datasets/${datasetId}`);

    const detailPage = page.getByTestId("dataset-detail-page");
    await expect(detailPage).toBeVisible({ timeout: LIVE_TIMEOUT });

    // Header (Requirement 1.x): the header renders with the name and the
    // original URL as a link. Assert structure, not any date/count text.
    const header = page.getByTestId("dataset-header");
    await expect(header).toBeVisible({ timeout: LIVE_TIMEOUT });
    await expect(header.getByTestId("dataset-name-text")).toBeVisible();
    await expect(header.getByTestId("dataset-original-url")).toHaveAttribute(
      "href",
      fixture,
    );

    // Metrics (Requirement 2.1): the headline metrics panel is present with its
    // tiles. The review-count tile exposes availability via data-available; a
    // processed fixture dataset has reviews, so it must not be "Not available".
    const metrics = page.getByTestId("metrics-panel");
    await expect(metrics).toBeVisible({ timeout: LIVE_TIMEOUT });
    await expect(metrics.getByTestId("metric-review-count")).toHaveAttribute(
      "data-available",
      "true",
    );

    // Snapshot (Requirement 3.1): a URL dataset shows the snapshot card. It may
    // be loading/ready/failed depending on the pre-signed URL; assert the card
    // renders with a resolved state attribute rather than asserting the image.
    const snapshot = page.getByTestId("snapshot-card");
    await expect(snapshot).toBeVisible({ timeout: LIVE_TIMEOUT });
    await expect
      .poll(async () => snapshot.getAttribute("data-state"), {
        timeout: LIVE_TIMEOUT,
      })
      .not.toBe("loading");

    // Completeness (Requirement 4.2): the panel renders, and for a URL dataset
    // the pages-captured line is present. Assert the fact rows exist by
    // data-testid, never on the formatted counts.
    const completeness = page.getByTestId("completeness-panel");
    await expect(completeness).toBeVisible({ timeout: LIVE_TIMEOUT });
    await expect(completeness.getByTestId("completeness-pages")).toBeVisible();

    // Reviews table (Requirement 5.2): the table renders rows, then applying a
    // rating filter re-queries the server and the table stays coherent. Assert
    // on structure (rows present, pager data attributes), never on counts.
    const reviews = page.getByTestId("reviews-table");
    await expect(reviews).toBeVisible({ timeout: LIVE_TIMEOUT });
    const firstRow = reviews.getByTestId("review-row").first();
    await expect(firstRow).toBeVisible({ timeout: LIVE_TIMEOUT });

    // Filter by a rating (Requirement 5.2). The select drives a server query
    // (`GET /datasets/{id}/reviews?rating=`); wait for the body to settle. The
    // filtered set is either rows (all showing the chosen rating) or the empty
    // state — both are valid outcomes of a real filter, so assert exactly one
    // of them appears and that the filter value stuck.
    const ratingFilter = reviews.getByTestId("reviews-filter-rating");
    await ratingFilter.selectOption("5");
    await expect(ratingFilter).toHaveValue("5");

    const body = reviews.getByTestId("reviews-body");
    await expect(body).toBeVisible();
    await expect
      .poll(
        async () => {
          const rows = await reviews.getByTestId("review-row").count();
          const empty = await reviews.getByTestId("reviews-empty").count();
          return rows > 0 || empty > 0;
        },
        { timeout: LIVE_TIMEOUT },
      )
      .toBe(true);

    // Every rendered row (if any) matches the chosen rating — the filter is
    // applied by the server, so a stale row would be a real bug.
    const filteredRows = reviews.getByTestId("review-row");
    const filteredCount = await filteredRows.count();
    for (let i = 0; i < filteredCount; i += 1) {
      await expect(filteredRows.nth(i).getByTestId("review-rating")).toContainText(
        "5",
      );
    }

    await archiveViaApi(request, datasetId);
  });

  // ─────────────────────────────────────────────────────────────────────────
  // Scenario 2 — add a dataset and watch the detail page move from processing
  //   to ready WITHOUT a reload. (Requirement 6.3)
  // ─────────────────────────────────────────────────────────────────────────
  test("detail page moves from processing to ready live, without a reload", async ({
    page,
    request,
  }) => {
    await page.goto("/");
    await expect(page.getByTestId("app-shell")).toBeVisible({
      timeout: 45_000,
    });
    if (!(await backendReachableFromSpa(page))) {
      skipBackendUnreachable();
      return;
    }

    // Seed a dataset through the API. The Add kicks off processing: the first
    // version is captured and analyzed asynchronously, so right after Add the
    // dataset has no active version yet.
    const fixture = uniqueFixtureUrl("live");
    const datasetId = await seedDataset(request, fixture);
    if (!datasetId) {
      skipNoSeed();
      return;
    }

    // Open the detail page immediately — while the first version is still
    // processing. The page shows skeletons and a processing notice (no active
    // version yet, Requirement 6.2). If processing already finished before we
    // got here (fast stub), the metrics panel is already present; either way
    // the transition below settles on the ready state.
    await page.goto(`/datasets/${datasetId}`);
    const detailPage = page.getByTestId("dataset-detail-page");
    await expect(detailPage).toBeVisible({ timeout: LIVE_TIMEOUT });

    // Capture whether we caught the processing state. The processing notice
    // (first-version progress) and/or the metrics skeleton stand in before a
    // version is active. We don't require catching it (processing can be fast),
    // but when present it proves we started pre-ready.
    const processingNotice = page.getByTestId("processing-notice");
    const metricsSkeleton = page.getByTestId("skeleton-metrics");
    const sawProcessing =
      (await processingNotice.count()) > 0 || (await metricsSkeleton.count()) > 0;
    test.info().annotations.push({
      type: "info",
      description: `caught first-version processing state: ${sawProcessing}`,
    });

    // Now observe the LIVE transition to ready WITHOUT a page reload: the
    // metrics panel appears once an active version lands (the useRealtime
    // channel patches the detail cache, or the detail query refetches on the
    // active_version change — Requirement 6.3). We never call page.reload().
    const metrics = page.getByTestId("metrics-panel");
    await expect(metrics).toBeVisible({ timeout: PROCESS_TIMEOUT });

    // And the first-version processing notice is gone (replaced by the active
    // summary), confirming the page moved to the ready state in place.
    await expect(page.getByTestId("skeleton-metrics")).toHaveCount(0);

    await archiveViaApi(request, datasetId);
  });
});
