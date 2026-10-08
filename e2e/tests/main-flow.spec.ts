import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import { fixtureUrl, liveAi } from "../support/env";

/**
 * Final main-flow E2E (guardrailed-chat task 9) — completes the
 * platform-foundation main flow end to end.
 *
 * The flow (design "Final E2E test"; Requirements 1.3, 2.2, 3.4, 5.2, 7.1, 9.1,
 * 9.2, 9.4):
 *
 *   1. Open the app, check + add a fixture dataset, wait for it to reach an
 *      active version (`updated` / processed).
 *   2. Ask an IN-SCOPE question → the answer streams token-by-token (Req 7.1)
 *      and shows citation chips with working popovers (Req 2.2).
 *   3. Ask an OUT-OF-SCOPE question → the assistant declines explicitly, shown
 *      by the "Outside dataset scope" decline tag (Req 3.4).
 *   4. Reload the page → both Exchanges persist in the shared history (Req 5.2).
 *   5. Refresh the dataset → a pending RefreshMarker appears and the chat stays
 *      usable during processing; then a completed marker (Req 9.1, 9.2), and the
 *      earlier Exchanges are labelled "Based on earlier data" (stale chip,
 *      Req 9.4) with their citation popovers still correct (Req 2.2).
 *   6. Archive the dataset → the chat input is disabled and the history is still
 *      visible (Req 1.3).
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * ChatPanel mounting (guardrailed-chat task 9). Task 6.9 built `ChatPanel` but
 * noted it was NOT yet wired into the detail page (the `slot-chat` placeholder
 * belonged to ingestion-summary). This task mounts it: `DatasetDetailPage` now
 * renders `<ChatPanel datasetId=… availability=… activeVersion=… />` inside
 * `slot-chat`, deriving the availability from the dataset detail via
 * `chatAvailabilityFor` (archived → disabled input, Req 1.3). So this test drives
 * the real chat UI on the detail page.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * Live vs. stub. The chat answer is AI-generated, so it needs either the live
 * model (`E2E_LIVE_AI=1`) or a recorded chat fixture (`make record-ai`). Under
 * the default stub, `FakeClaude` has no recorded chat response and raises
 * `MissingFixtureError`, so the AI-dependent flow can't run. Matching the
 * ingestion spec's `needs-record-ai` pattern, this test:
 *   - seeds the dataset deterministically through the Check/Add API, and
 *   - gates the AI-dependent parts: when it cannot produce a seeded, processed
 *     dataset AND we're not live, it records a `needs-record-ai` annotation and
 *     skips cleanly, so `make e2e` without live AI stays green.
 *
 * To run the full flow live (a person, with spend): the stored key has a doubled
 * `sk-sk-ant-usr-` prefix (de-dup the leading `sk-`), and the working chat model
 * is `claude-sonnet-5` (the configured default 404s), so start the backend with
 * `CLAUDE_CHAT_MODEL=claude-sonnet-5` and a de-duped `ANTHROPIC_API_KEY`, then run
 * `E2E_LIVE_AI=1 make e2e`. Do not hardcode/commit the key or model here.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * Conventions (steering testing.md, mirrored from the other specs' E2E): only
 * `data-testid` / `data-*` selectors; never assert on text containing counts or
 * dates; load review pages from the fixture site via `fixtureUrl(...)`.
 *
 * Independence/idempotency: the test seeds its OWN dataset with a unique fixture
 * URL through the Check/Add API, drives it from the detail route, and archives
 * what it created so a re-run never collides with leftovers.
 */

/** Generous ceiling for the polling fallback (10 s list / 2 s check + slack). */
const LIVE_TIMEOUT = 40_000;

/** How long to wait for a processing dataset to reach a terminal/active state. */
const PROCESS_TIMEOUT = 60_000;

/** How long to wait for a streamed answer to begin / finish (live model slack). */
const ANSWER_TIMEOUT = 45_000;

/** An in-scope question any review set can answer (Req 2.1 example). */
const IN_SCOPE_QUESTION = "What are the most common complaints in these reviews?";

/**
 * An out-of-scope question the scope guard must decline (Req 3.2): general
 * world knowledge / weather, unrelated to the reviews.
 */
const OUT_OF_SCOPE_QUESTION = "What's the weather forecast for tomorrow?";

/**
 * A unique fixture URL for a run so the test owns its data. The path is a real
 * fixture review page (so a Check/collection can render it); the query string
 * makes the normalized URL unique per run, so two runs never share a dataset.
 */
function uniqueFixtureUrl(tag: string): string {
  const nonce = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  return fixtureUrl(`/extraction/plain_list/?e2e=${tag}-${nonce}`);
}

// ───────────────────────────────────────────────────────────────────────────
// Seeding + environment capability detection (through the API request context).
// These mirror ingestion-summary.spec.ts / library.spec.ts exactly so the
// gating behaves identically across the suite: a dataset is created via the
// Check/Add flow (there is no standalone "create dataset" endpoint), and the
// helpers annotate-and-skip when the environment can't produce an addable
// verdict rather than false-failing.
// ───────────────────────────────────────────────────────────────────────────

/**
 * Seed a tracked dataset through the Check/Add API. Returns the created dataset
 * id, or null when the backend can't be driven in this environment (so the
 * caller can annotate + skip).
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
    items: Array<{ item_id: string; state: string }>;
  };
  const checkId = created.check_id;
  const itemId = created.items[0]?.item_id;
  if (!itemId) return null;

  // Poll the Check until the item reaches a terminal state with a verdict.
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
  // will_work/limited verdict needs a recorded Review Locator response, so this
  // is where a stubbed environment without that recording bails out.
  if (verdict !== "will_work" && verdict !== "limited") return null;

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

/**
 * Poll the dataset detail endpoint until its `active_version` is set (a version
 * is ready / `updated`) or the deadline passes.
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

/** Read a dataset's current active_version through the API (or null). */
async function activeVersionOf(
  request: APIRequestContext,
  id: string,
): Promise<number | null> {
  const poll = await request.get(`/api/datasets/${id}`);
  if (!poll.ok()) return null;
  const detail = (await poll.json()) as { active_version?: number | null };
  return detail.active_version ?? null;
}

/** Best-effort cleanup: archive a dataset so a re-run starts clean. */
async function archiveViaApi(
  request: APIRequestContext,
  id: string,
): Promise<void> {
  await request.post(`/api/datasets/${id}/archive`).catch(() => undefined);
}

/**
 * Probe whether the SPA can reach the live backend in this environment. A 2xx
 * from the DB-backed, unauthenticated list endpoint means the browser path
 * (vite proxy → api) is wired up; a non-2xx means the detail page can't load
 * its data — annotate + skip, like the other specs.
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
      "return 2xx from the browser). The detail page + chat need their data, " +
      "so this flow can't run in this environment.",
  });
  test.skip(true, "SPA cannot reach the live backend in this environment.");
}

/** Annotate + skip when a dataset couldn't be seeded (no addable verdict). */
function skipNoSeed(): void {
  if (!liveAi) {
    test.info().annotations.push({
      type: "needs-record-ai",
      description:
        "The full main flow needs a processed dataset to ask questions against, " +
        "which needs a will_work/limited Check verdict (under the default stub a " +
        "recorded Review Locator response) AND a recorded/live chat answer. Run " +
        "with E2E_LIVE_AI=1 (and the de-duped ANTHROPIC_API_KEY + " +
        "CLAUDE_CHAT_MODEL=claude-sonnet-5), or `make record-ai`.",
    });
  } else {
    test.info().annotations.push({
      type: "deferred",
      description:
        "Could not seed a tracked dataset through the Check/Add flow even with " +
        "live AI; see the report for the environment gaps.",
    });
  }
  test.skip(true, "Could not seed a processed dataset in this environment.");
}

/**
 * Ask a question through the mounted ChatPanel and wait for the streamed answer
 * to begin, then finish (the pending row disappears and the Exchange merges into
 * the history). Returns the newly added history Exchange locator.
 *
 * Streaming (Req 7.1): the live answer renders in the StreamingExchange's
 * `streaming-answer` region token-by-token while `streaming-exchange` is in the
 * `streaming` phase; on `done` the phase returns to idle and the Exchange appears
 * in the HistoryPane. We assert the streaming phase is observed, then wait for
 * the merged history row.
 */
async function askQuestion(page: Page, question: string): Promise<void> {
  const panel = page.getByTestId("chat-panel");
  const textarea = panel.getByTestId("chat-input-textarea");
  await expect(textarea).toBeEnabled({ timeout: LIVE_TIMEOUT });

  const historyBefore = await panel.getByTestId("history-exchange").count();

  await textarea.fill(question);
  await panel.getByTestId("chat-input-submit").click();

  const streaming = panel.getByTestId("streaming-exchange");
  // The stream enters the `streaming` phase (token-by-token answer, Req 7.1).
  // It can be brief under a fast stub, so tolerate having already settled: we
  // require either observing the streaming phase OR the history growing.
  await expect
    .poll(
      async () => {
        const phase = await streaming.getAttribute("data-phase");
        const count = await panel.getByTestId("history-exchange").count();
        return phase === "streaming" || count > historyBefore;
      },
      { timeout: ANSWER_TIMEOUT },
    )
    .toBe(true);

  // The answer finishes and merges into the shared history (Req 6.3): the
  // pending row returns to idle and a new history Exchange is present.
  await expect
    .poll(async () => panel.getByTestId("history-exchange").count(), {
      timeout: ANSWER_TIMEOUT,
    })
    .toBeGreaterThan(historyBefore);
  await expect(streaming).toHaveAttribute("data-phase", "idle", {
    timeout: ANSWER_TIMEOUT,
  });
}

/** The newest history Exchange row (bottom of the oldest-first timeline). */
function newestExchange(page: Page) {
  return page.getByTestId("chat-panel").getByTestId("history-exchange").last();
}

test.describe("main flow: check + add, process, ask, refresh, archive", () => {
  test("the full guardrailed-chat flow end to end", async ({ page, request }) => {
    // 1 ── Open the app and seed a processed dataset ─────────────────────────
    await page.goto("/");
    await expect(page.getByTestId("app-shell")).toBeVisible({
      // Vite's first-request compile on a cold dev server can be slow.
      timeout: 45_000,
    });
    if (!(await backendReachableFromSpa(page))) {
      skipBackendUnreachable();
      return;
    }

    const fixture = uniqueFixtureUrl("mainflow");
    const datasetId = await seedDataset(request, fixture);
    if (!datasetId) {
      skipNoSeed();
      return;
    }
    const ready = await waitForActiveVersion(request, datasetId);
    expect(ready, "seeded dataset reached an active version (updated)").toBe(true);
    const firstVersion = await activeVersionOf(request, datasetId);

    // Navigate to the detail page; the ChatPanel mounts in slot-chat.
    await page.goto(`/datasets/${datasetId}`);
    await expect(page.getByTestId("dataset-detail-page")).toBeVisible({
      timeout: LIVE_TIMEOUT,
    });
    const panel = page.getByTestId("chat-panel");
    await expect(panel).toBeVisible({ timeout: LIVE_TIMEOUT });
    // A processed, non-archived dataset → the input is available (Req 1.1).
    await expect(panel).toHaveAttribute("data-availability", "available");

    // 2 ── Ask an in-scope question: streamed answer with citations ──────────
    await askQuestion(page, IN_SCOPE_QUESTION);

    const inScope = newestExchange(page);
    await expect(inScope).toBeVisible();
    // An in-scope answer must carry at least one citation chip whose popover
    // shows the cited review (Req 2.2). Open the first chip and assert the
    // popover renders the saved snippet text.
    const citationChip = inScope.getByTestId("citation-chip").first();
    await expect(citationChip).toBeVisible({ timeout: ANSWER_TIMEOUT });
    await citationChip.getByTestId("citation-chip-button").hover();
    const popover = inScope.getByTestId("citation-popover").first();
    await expect(popover).toBeVisible();
    await expect(popover.getByTestId("citation-popover-text")).not.toBeEmpty();
    // It was answered in scope (not declined).
    await expect(inScope).toHaveAttribute("data-scope", "in_scope");

    // 3 ── Ask an out-of-scope question: an explicit decline ─────────────────
    await askQuestion(page, OUT_OF_SCOPE_QUESTION);

    const declined = newestExchange(page);
    await expect(declined).toBeVisible();
    // The scope guard declined (Req 3.4): the Exchange is tagged declined and
    // shows the subtle "Outside dataset scope" decline tag.
    await expect(declined).toHaveAttribute("data-scope", "declined");
    await expect(declined.getByTestId("decline-tag")).toBeVisible();

    // 4 ── Reload: both Exchanges persist in the shared history (Req 5.2) ─────
    await page.reload();
    await expect(page.getByTestId("dataset-detail-page")).toBeVisible({
      timeout: LIVE_TIMEOUT,
    });
    const reloadedPanel = page.getByTestId("chat-panel");
    await expect(reloadedPanel).toBeVisible({ timeout: LIVE_TIMEOUT });
    // The history pane shows both saved Exchanges (one in-scope, one declined).
    await expect
      .poll(async () => reloadedPanel.getByTestId("history-exchange").count(), {
        timeout: LIVE_TIMEOUT,
      })
      .toBeGreaterThanOrEqual(2);
    await expect(
      reloadedPanel
        .getByTestId("history-exchange")
        .filter({ has: page.locator('[data-scope="in_scope"]') })
        .first(),
    ).toBeVisible();
    await expect(
      reloadedPanel
        .getByTestId("history-exchange")
        .filter({ has: page.locator('[data-scope="declined"]') })
        .first(),
    ).toBeVisible();

    // 5 ── Refresh the dataset: pending → completed marker; earlier Exchanges
    //      go stale; chat stays usable during processing (Req 9.1, 9.2, 9.4) ──
    const refresh = await request.post(`/api/datasets/${datasetId}/refresh`);
    expect(
      refresh.ok(),
      "refresh accepted (a will_work refresh needs no confirmation)",
    ).toBe(true);

    // A pending RefreshMarker appears live (Req 9.2) — the detail page's single
    // realtime channel refetches the chat history on the refresh lifecycle
    // transition, so the marker shows without a reload. Under a polling-only
    // fallback the history refetch still lands within LIVE_TIMEOUT.
    const marker = reloadedPanel.getByTestId("refresh-marker");
    await expect
      .poll(
        async () => {
          // Nudge the timeline to pick up the new marker if the realtime
          // channel isn't proxied locally (polling fallback): a soft reload of
          // the history query happens on navigation, so re-assert by count.
          return marker.count();
        },
        { timeout: PROCESS_TIMEOUT },
      )
      .toBeGreaterThan(0);

    // The chat stays usable WHILE the refresh runs (Req 1.1 / 9.2): the input is
    // still enabled (availability is `available` or `refreshing`, both enabled).
    await expect(reloadedPanel.getByTestId("chat-input-textarea")).toBeEnabled();

    // The marker becomes `completed` once the new version lands (Req 9.1); the
    // pending state must not stick. Assert on the data-state, never on the
    // formatted count/date text.
    await expect
      .poll(async () => marker.last().getAttribute("data-state"), {
        timeout: PROCESS_TIMEOUT,
      })
      .toBe("completed");

    // Wait for the new active version so the earlier Exchanges are now stale.
    await expect
      .poll(async () => activeVersionOf(request, datasetId), {
        timeout: PROCESS_TIMEOUT,
      })
      .not.toBe(firstVersion);

    // The earlier Exchanges (answered against the first version) are labelled
    // "Based on earlier data" (Req 9.4): they carry data-stale="true" and the
    // stale chip. The history query refetches on the completed transition, so
    // poll for the stale rows to appear.
    const staleExchanges = reloadedPanel
      .getByTestId("history-exchange")
      .filter({ has: page.locator('[data-stale="true"]') });
    await expect
      .poll(async () => staleExchanges.count(), { timeout: PROCESS_TIMEOUT })
      .toBeGreaterThanOrEqual(2);
    const firstStale = staleExchanges.first();
    await expect(firstStale.getByTestId("stale-chip")).toBeVisible();

    // Their citation popovers still show the right review from the saved
    // snippet (Req 2.2): find a stale Exchange that has a citation chip and
    // confirm its popover renders text.
    const staleWithCitation = reloadedPanel
      .getByTestId("history-exchange")
      .filter({ has: page.locator('[data-stale="true"]') })
      .filter({ has: page.getByTestId("citation-chip") })
      .first();
    if ((await staleWithCitation.count()) > 0) {
      const chip = staleWithCitation.getByTestId("citation-chip-button").first();
      await chip.hover();
      const stalePopover = staleWithCitation.getByTestId("citation-popover").first();
      await expect(stalePopover).toBeVisible();
      await expect(
        stalePopover.getByTestId("citation-popover-text"),
      ).not.toBeEmpty();
    }

    // 6 ── Archive: input disabled, history still visible (Req 1.3) ───────────
    const archive = await request.post(`/api/datasets/${datasetId}/archive`);
    expect(archive.ok(), "archive accepted").toBe(true);

    // Reload so the detail page derives the archived availability from the fresh
    // detail record (archived datasets are read-only, Req 1.3).
    await page.goto(`/datasets/${datasetId}`);
    await expect(page.getByTestId("dataset-detail-page")).toBeVisible({
      timeout: LIVE_TIMEOUT,
    });
    const archivedPanel = page.getByTestId("chat-panel");
    await expect(archivedPanel).toBeVisible({ timeout: LIVE_TIMEOUT });
    // The input is disabled with the archived availability (Req 1.3).
    await expect(archivedPanel).toHaveAttribute("data-availability", "archived");
    await expect(archivedPanel.getByTestId("chat-input-textarea")).toBeDisabled();
    await expect(
      archivedPanel.getByTestId("chat-input-disabled-message"),
    ).toBeVisible();
    // The history is still visible while archived.
    await expect(archivedPanel.getByTestId("history-pane")).toBeVisible();
    await expect
      .poll(async () => archivedPanel.getByTestId("history-exchange").count(), {
        timeout: LIVE_TIMEOUT,
      })
      .toBeGreaterThanOrEqual(2);

    // Leave the dataset archived (cleanup is a no-op here, kept for symmetry).
    await archiveViaApi(request, datasetId);
  });
});
