/**
 * Component tests for {@link DatasetDetailPage} (ingestion-summary task 2.1).
 *
 * Scope for this task is the route *shell* and the not-found state
 * (Requirement 1.4; design "Error Handling"). The per-component coverage lands
 * in task 7. Here we check:
 *   - the detail query (`GET /datasets/{id}`) is fetched on `['dataset', id]`
 *     and the responsive layout scaffold (both columns + the placeholder slots)
 *     renders once it resolves;
 *   - a 404 shows "Dataset not found" with a link back to the Library;
 *   - a non-404 error shows an error state (not the summary layout);
 *   - the Library link navigates via the injected `onNavigate`.
 *
 * The API is mocked with MSW (`src/test/server.ts`); counts/dates are never
 * asserted via text (testing.md).
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DatasetDetail } from "../api/datasets";
import {
  makeDatasetDetail,
  makeHistoryPage,
  makeReviewsPage,
  makeSnapshotUrl,
  makeSuggestions,
} from "../test/fixtures";
import { datasetQueryKey } from "../hooks/realtimeReducers";
import type { DatasetStatusChangedFrame } from "../hooks/realtimeTypes";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import {
  installMockWebSocket,
  lastMockWebSocket,
  uninstallMockWebSocket,
} from "../test/ws";
import DatasetDetailPage from "./DatasetDetailPage";

/**
 * Stub `GET /api/datasets/:id` with the given detail record, plus permissive
 * handlers for the sub-resources the summary components fetch once the detail
 * resolves — the snapshot URL ({@link SnapshotCard}) and the reviews page
 * ({@link ReviewsTable}, wired into `slot-reviews` in task 5). The shell tests
 * only assert the layout scaffold, but mounting those children fires their
 * queries, so these keep the `onUnhandledRequest: "error"` harness quiet.
 */
function stubDetail(id: string, detail: DatasetDetail): void {
  server.use(
    http.get(`/api/datasets/${id}`, () => HttpResponse.json(detail, { status: 200 })),
    http.get(`/api/datasets/${id}/snapshot-url`, () =>
      HttpResponse.json(makeSnapshotUrl(), { status: 200 }),
    ),
    http.get(`/api/datasets/${id}/reviews`, () =>
      HttpResponse.json(makeReviewsPage([]), { status: 200 }),
    ),
    // The ChatPanel mounted in slot-chat (guardrailed-chat task 9) fires these
    // once the detail loads; stub them so the error harness stays quiet.
    http.get(`/api/datasets/${id}/chat/history`, () =>
      HttpResponse.json(makeHistoryPage([]), { status: 200 }),
    ),
    http.get(`/api/datasets/${id}/chat/suggestions`, () =>
      HttpResponse.json(makeSuggestions(), { status: 200 }),
    ),
  );
}

/** Stub `GET /api/datasets/:id` with a `{ error: { code, message } }` envelope. */
function stubError(id: string, status: number, code: string, message: string): void {
  server.use(
    http.get(`/api/datasets/${id}`, () =>
      HttpResponse.json({ error: { code, message } }, { status }),
    ),
  );
}

function renderPage(id = "ds-1", onNavigate = vi.fn()) {
  return {
    onNavigate,
    // The shell/not-found/error cases don't exercise the live channel; keep it
    // off so they don't open a WebSocket. The live-update suite below enables it.
    ...renderWithClient(
      <DatasetDetailPage datasetId={id} onNavigate={onNavigate} realtimeEnabled={false} />,
    ),
  };
}

beforeEach(() => {
  window.history.pushState(null, "", "/datasets/ds-1");
});

afterEach(() => {
  window.history.pushState(null, "", "/");
});

describe("DatasetDetailPage layout (Requirement 1.4)", () => {
  it("fetches the detail and renders the full-width top plus both columns", async () => {
    stubDetail("ds-1", makeDatasetDetail({ id: "ds-1" }));
    renderPage("ds-1");

    const page = await screen.findByTestId("dataset-detail-page");
    // Full-width header + readiness run across the top.
    const top = within(page).getByTestId("detail-top");
    expect(within(top).getByTestId("slot-header")).toBeInTheDocument();
    expect(within(top).getByTestId("slot-readiness")).toBeInTheDocument();

    // Two columns below.
    expect(within(page).getByTestId("detail-column-left")).toBeInTheDocument();
    expect(within(page).getByTestId("detail-column-right")).toBeInTheDocument();
  });

  it("places the metric slots on the left and snapshot/completeness/prediction on the right", async () => {
    stubDetail("ds-1", makeDatasetDetail({ id: "ds-1" }));
    renderPage("ds-1");

    const left = await screen.findByTestId("detail-column-left");
    const right = screen.getByTestId("detail-column-right");

    for (const slot of ["slot-metrics", "slot-rating-chart", "slot-entity", "slot-themes"]) {
      expect(within(left).getByTestId(slot)).toBeInTheDocument();
    }
    for (const slot of ["slot-snapshot", "slot-completeness", "slot-prediction"]) {
      expect(within(right).getByTestId(slot)).toBeInTheDocument();
    }
  });

  it("renders the timeline, reviews, and chat regions below the columns", async () => {
    stubDetail("ds-1", makeDatasetDetail({ id: "ds-1" }));
    renderPage("ds-1");

    const page = await screen.findByTestId("dataset-detail-page");
    expect(within(page).getByTestId("slot-timeline")).toBeInTheDocument();
    expect(within(page).getByTestId("slot-reviews")).toBeInTheDocument();
    expect(within(page).getByTestId("slot-chat")).toBeInTheDocument();
  });

  it("marks the page busy while the detail query is loading", async () => {
    // Never-resolving handler keeps the query in the loading state.
    server.use(
      http.get("/api/datasets/ds-1", () => new Promise<Response>(() => {})),
    );
    renderPage("ds-1");

    const page = await screen.findByTestId("dataset-detail-page");
    expect(page).toHaveAttribute("data-loading", "true");
    expect(page).toHaveAttribute("aria-busy", "true");
  });
});

describe("Not-found state (Requirement 1.4; design Error Handling)", () => {
  it("shows 'Dataset not found' with a link back to the Library on a 404", async () => {
    stubError("missing", 404, "NOT_FOUND", "No such dataset");
    renderPage("missing");

    const notFound = await screen.findByTestId("dataset-not-found");
    expect(within(notFound).getByRole("heading", { name: /dataset not found/i })).toBeInTheDocument();

    const link = within(notFound).getByTestId("not-found-library-link");
    expect(link).toHaveAttribute("href", "/");

    // The summary layout is not rendered.
    expect(screen.queryByTestId("detail-columns")).not.toBeInTheDocument();
  });

  it("navigates to the Library when the not-found link is clicked", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    stubError("missing", 404, "NOT_FOUND", "No such dataset");
    renderPage("missing", onNavigate);

    const link = await screen.findByTestId("not-found-library-link");
    await user.click(link);
    expect(onNavigate).toHaveBeenCalledWith("/");
  });
});

describe("Generic error state", () => {
  it("shows an error (not the not-found or summary layout) on a non-404 failure", async () => {
    stubError("ds-1", 500, "SERVER_ERROR", "boom");
    renderPage("ds-1");

    const error = await screen.findByTestId("dataset-detail-error");
    expect(error).toBeInTheDocument();
    expect(screen.queryByTestId("dataset-not-found")).not.toBeInTheDocument();
    expect(screen.queryByTestId("detail-columns")).not.toBeInTheDocument();
  });

  it("retries the detail query from the error state", async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(
      http.get("/api/datasets/ds-1", () => {
        calls += 1;
        if (calls === 1) {
          return HttpResponse.json(
            { error: { code: "SERVER_ERROR", message: "boom" } },
            { status: 500 },
          );
        }
        return HttpResponse.json(makeDatasetDetail({ id: "ds-1" }), { status: 200 });
      }),
      // Sub-resources fetched once the detail resolves after the retry (incl.
      // the ChatPanel's history + suggestions, guardrailed-chat task 9).
      http.get("/api/datasets/ds-1/snapshot-url", () =>
        HttpResponse.json(makeSnapshotUrl(), { status: 200 }),
      ),
      http.get("/api/datasets/ds-1/reviews", () =>
        HttpResponse.json(makeReviewsPage([]), { status: 200 }),
      ),
      http.get("/api/datasets/ds-1/chat/history", () =>
        HttpResponse.json(makeHistoryPage([]), { status: 200 }),
      ),
      http.get("/api/datasets/ds-1/chat/suggestions", () =>
        HttpResponse.json(makeSuggestions(), { status: 200 }),
      ),
    );
    renderPage("ds-1");

    await user.click(await screen.findByTestId("dataset-detail-retry"));
    await waitFor(() => expect(screen.getByTestId("detail-columns")).toBeInTheDocument());
  });
});

describe("Live active_version change (Requirement 6.3; task 6.2)", () => {
  beforeEach(() => {
    installMockWebSocket();
  });

  afterEach(() => {
    uninstallMockWebSocket();
  });

  /**
   * Build a `dataset.status.changed` frame that moves the dataset to a new
   * active version — the exact shape the dataset-library reducer patches onto
   * `['dataset', id]`.
   */
  function versionFrame(id: string, activeVersion: number): DatasetStatusChangedFrame {
    return {
      type: "dataset.status.changed",
      dataset_id: id,
      status: "updated",
      at: "2026-09-02T21:00:00Z",
      data_version: activeVersion,
      active_version: activeVersion,
      message: "New version ready",
      metrics: { review_count: 300 },
    };
  }

  it("refetches the reviews and snapshot for the new version when active_version changes", async () => {
    const id = "ds-live";
    let reviewsCalls = 0;
    let snapshotCalls = 0;
    // The version the server currently serves across the detail/snapshot
    // endpoints; bumped to 4 when the dataset moves on so the refetch picks up
    // the new version (what a real backend would return after the transition).
    let currentVersion = 3;

    server.use(
      http.get(`/api/datasets/${id}`, () =>
        HttpResponse.json(
          makeDatasetDetail({
            id,
            active_version: currentVersion,
            data_version: currentVersion,
          }),
          { status: 200 },
        ),
      ),
      http.get(`/api/datasets/${id}/reviews`, () => {
        reviewsCalls += 1;
        return HttpResponse.json(makeReviewsPage([]), { status: 200 });
      }),
      http.get(`/api/datasets/${id}/snapshot-url`, () => {
        snapshotCalls += 1;
        return HttpResponse.json(makeSnapshotUrl({ version: currentVersion }), {
          status: 200,
        });
      }),
      // The ChatPanel's history is also refetched when the realtime frame lands
      // (realtimeReducers invalidates ['chat-history', id]); stub it + the
      // suggestions so the error harness stays quiet (guardrailed-chat task 9).
      http.get(`/api/datasets/${id}/chat/history`, () =>
        HttpResponse.json(makeHistoryPage([]), { status: 200 }),
      ),
      http.get(`/api/datasets/${id}/chat/suggestions`, () =>
        HttpResponse.json(makeSuggestions(), { status: 200 }),
      ),
    );

    const { client } = renderWithClient(
      <DatasetDetailPage datasetId={id} onNavigate={vi.fn()} realtimeEnabled />,
    );

    // Page loads at v3: reviews and snapshot each fetched once.
    await screen.findByTestId("detail-columns");
    await waitFor(() => {
      expect(reviewsCalls).toBe(1);
      expect(snapshotCalls).toBe(1);
    });

    const reviewsAtV3 = reviewsCalls;
    const snapshotAtV3 = snapshotCalls;

    // The real-time channel opens; drive a frame that moves the dataset to v4.
    const socket = lastMockWebSocket();
    expect(socket).toBeDefined();
    act(() => socket!.simulateOpen());

    // From here on, the server serves v4 across detail + snapshot.
    currentVersion = 4;
    act(() => socket!.simulateMessage(versionFrame(id, 4)));

    // The reducer patched the detail cache to active_version 4; the detail-page
    // effect then invalidates reviews + snapshot so the new version reloads.
    await waitFor(() => {
      expect(reviewsCalls).toBeGreaterThan(reviewsAtV3);
      expect(snapshotCalls).toBeGreaterThan(snapshotAtV3);
    });

    // The new version's snapshot is what the cache now holds.
    await waitFor(() => {
      const snap = client.getQueryData<{ version: number }>(["snapshot", id]);
      expect(snap?.version).toBe(4);
    });
    // The detail cache reflects the new active version too.
    const detail = client.getQueryData<DatasetDetail>(datasetQueryKey(id));
    expect(detail?.active_version).toBe(4);
  });

  it("does not refetch reviews or snapshot when a frame keeps the same active_version", async () => {
    const id = "ds-same";
    let reviewsCalls = 0;
    let snapshotCalls = 0;

    server.use(
      http.get(`/api/datasets/${id}`, () =>
        HttpResponse.json(makeDatasetDetail({ id, active_version: 3, data_version: 3 }), {
          status: 200,
        }),
      ),
      http.get(`/api/datasets/${id}/reviews`, () => {
        reviewsCalls += 1;
        return HttpResponse.json(makeReviewsPage([]), { status: 200 });
      }),
      http.get(`/api/datasets/${id}/snapshot-url`, () => {
        snapshotCalls += 1;
        return HttpResponse.json(makeSnapshotUrl({ version: 3 }), { status: 200 });
      }),
      // The ChatPanel's history is refetched on the realtime frame; stub it +
      // suggestions so the error harness stays quiet (guardrailed-chat task 9).
      http.get(`/api/datasets/${id}/chat/history`, () =>
        HttpResponse.json(makeHistoryPage([]), { status: 200 }),
      ),
      http.get(`/api/datasets/${id}/chat/suggestions`, () =>
        HttpResponse.json(makeSuggestions(), { status: 200 }),
      ),
    );

    renderWithClient(
      <DatasetDetailPage datasetId={id} onNavigate={vi.fn()} realtimeEnabled />,
    );

    await screen.findByTestId("detail-columns");
    await waitFor(() => {
      expect(reviewsCalls).toBe(1);
      expect(snapshotCalls).toBe(1);
    });

    const socket = lastMockWebSocket();
    act(() => socket!.simulateOpen());
    // A progress frame for the SAME active version (v3): status moved but no
    // new version. No reviews/snapshot refetch should follow.
    act(() => socket!.simulateMessage(versionFrame(id, 3)));

    // Give the effect a chance to run, then assert the counts are unchanged.
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(reviewsCalls).toBe(1);
    expect(snapshotCalls).toBe(1);
  });
});
