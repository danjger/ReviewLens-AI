/**
 * Component tests for the detail page's live states (ingestion-summary
 * task 6.3, Requirements 6.2 and 6.4).
 *
 * These drive {@link DatasetDetailPage} through its three live states and
 * assert the DOM wiring on top of the pure {@link deriveDetailPageState}
 * helper (unit-tested separately):
 *
 *   - **first-version processing** — the metric/snapshot/reviews slots show
 *     skeletons, the latest `last_message` shows as a progress line, and the
 *     chat slot is marked disabled;
 *   - **refresh in progress** — an active version keeps data (metrics panel)
 *     and the chat available;
 *   - **failed, no active version** — the failure message and a Refresh action
 *     show (and the Refresh posts to the refresh endpoint);
 *   - **failed, earlier version active** — "still showing v{n}" shows and the
 *     active version's data stays available.
 *
 * The API is mocked with MSW; selectors are `data-testid` based and never
 * assert on counts/dates (testing.md).
 */
import { screen, waitFor } from "@testing-library/react";
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
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import DatasetDetailPage from "./DatasetDetailPage";

/**
 * Stub `GET /api/datasets/:id` with the given detail record, plus permissive
 * handlers for the snapshot and reviews sub-resources the summary mounts (so
 * the `onUnhandledRequest: "error"` harness stays quiet even when a version is
 * active and those children fire their queries).
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
    // once the detail loads, in every live state; stub them so the error
    // harness stays quiet.
    http.get(`/api/datasets/${id}/chat/history`, () =>
      HttpResponse.json(makeHistoryPage([]), { status: 200 }),
    ),
    http.get(`/api/datasets/${id}/chat/suggestions`, () =>
      HttpResponse.json(makeSuggestions(), { status: 200 }),
    ),
  );
}

function renderPage(id = "ds-1") {
  // These states are driven straight off the detail record; the live channel
  // isn't needed, so keep it off (no WebSocket).
  return renderWithClient(
    <DatasetDetailPage datasetId={id} onNavigate={vi.fn()} realtimeEnabled={false} />,
  );
}

beforeEach(() => {
  window.history.pushState(null, "", "/datasets/ds-1");
});

afterEach(() => {
  window.history.pushState(null, "", "/");
});

describe("First-version processing (Requirement 6.2)", () => {
  /** A dataset whose first version is still being built: no active version. */
  function processingDetail(id: string): DatasetDetail {
    return makeDatasetDetail({
      id,
      status: "processing",
      display_state: "processing",
      active_version: null,
      data_version: null,
      metrics: null,
      review_count: null,
      last_message: "Fetching page 2 of 10",
    });
  }

  it("shows skeletons in the metric, snapshot, and reviews slots", async () => {
    stubDetail("ds-1", processingDetail("ds-1"));
    renderPage("ds-1");

    // The real panels are replaced by skeletons while no version is active.
    expect(await screen.findByTestId("skeleton-metrics")).toBeInTheDocument();
    expect(screen.getByTestId("skeleton-snapshot")).toBeInTheDocument();
    expect(screen.getByTestId("skeleton-reviews")).toBeInTheDocument();
    // …and the live panels are NOT mounted (so they don't show stale zeros).
    expect(screen.queryByTestId("metrics-panel")).not.toBeInTheDocument();
    expect(screen.queryByTestId("snapshot-card")).not.toBeInTheDocument();
  });

  it("shows the latest progress message", async () => {
    stubDetail("ds-1", processingDetail("ds-1"));
    renderPage("ds-1");

    const notice = await screen.findByTestId("processing-notice");
    expect(notice).toHaveAttribute("data-phase", "first_version_processing");
    expect(screen.getByTestId("processing-message")).toHaveTextContent(
      "Fetching page 2 of 10",
    );
  });

  it("marks the chat slot disabled", async () => {
    stubDetail("ds-1", processingDetail("ds-1"));
    renderPage("ds-1");

    // Wait for the detail to load (the progress notice appears) before reading
    // the chat slot's derived disabled flag.
    await screen.findByTestId("processing-notice");
    const chat = screen.getByTestId("slot-chat");
    expect(chat).toHaveAttribute("data-chat-disabled", "true");
    expect(chat).toHaveAttribute("aria-disabled", "true");
  });
});

describe("Refresh in progress keeps data + chat available (Requirement 6.2)", () => {
  it("renders the metrics panel and leaves the chat enabled while a refresh runs", async () => {
    stubDetail(
      "ds-1",
      makeDatasetDetail({
        id: "ds-1",
        status: "processing",
        display_state: "ready_refreshing",
        active_version: 2,
        data_version: 2,
      }),
    );
    renderPage("ds-1");

    // The active version's data stays available — no skeletons, real panel.
    expect(await screen.findByTestId("metrics-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("skeleton-metrics")).not.toBeInTheDocument();

    // The chat stays usable.
    const chat = screen.getByTestId("slot-chat");
    expect(chat).toHaveAttribute("data-chat-disabled", "false");
    expect(chat).toHaveAttribute("aria-disabled", "false");

    // No failure/progress notice in this state.
    expect(screen.queryByTestId("processing-notice")).not.toBeInTheDocument();
  });
});

describe("Failed with no active version (Requirement 6.4)", () => {
  function failedDetail(id: string): DatasetDetail {
    return makeDatasetDetail({
      id,
      status: "failed",
      display_state: "failed",
      active_version: null,
      data_version: null,
      metrics: null,
      last_message: "Page failed to load",
    });
  }

  it("shows the failure message and a Refresh action", async () => {
    stubDetail("ds-1", failedDetail("ds-1"));
    renderPage("ds-1");

    const notice = await screen.findByTestId("processing-notice");
    expect(notice).toHaveAttribute("data-phase", "failed_no_version");
    expect(screen.getByTestId("failure-message")).toHaveTextContent(
      "Page failed to load",
    );
    expect(screen.getByTestId("failure-refresh")).toBeInTheDocument();
    // No earlier version → no "still showing" line, and chat stays disabled.
    expect(screen.queryByTestId("still-showing-version")).not.toBeInTheDocument();
    expect(screen.getByTestId("slot-chat")).toHaveAttribute(
      "data-chat-disabled",
      "true",
    );
  });

  it("posts to the refresh endpoint when Refresh is clicked", async () => {
    const user = userEvent.setup();
    let refreshCalls = 0;
    stubDetail("ds-1", failedDetail("ds-1"));
    server.use(
      http.post("/api/datasets/ds-1/refresh", () => {
        refreshCalls += 1;
        return HttpResponse.json({ check_id: "chk-1" }, { status: 202 });
      }),
    );
    renderPage("ds-1");

    await user.click(await screen.findByTestId("failure-refresh"));
    await waitFor(() => expect(refreshCalls).toBe(1));
  });
});

describe("Failed with an earlier active version (Requirement 6.4)", () => {
  it("shows 'still showing v{n}' and keeps the active version's data available", async () => {
    stubDetail(
      "ds-1",
      makeDatasetDetail({
        id: "ds-1",
        status: "failed",
        display_state: "ready_refresh_failed",
        active_version: 2,
        data_version: 2,
        last_message: "Refresh failed: timeout",
      }),
    );
    renderPage("ds-1");

    const notice = await screen.findByTestId("processing-notice");
    expect(notice).toHaveAttribute("data-phase", "failed_showing_previous");

    const stillShowing = screen.getByTestId("still-showing-version");
    expect(stillShowing).toHaveAttribute("data-version", "2");
    expect(screen.getByTestId("failure-refresh")).toBeInTheDocument();

    // The earlier version's data stays available (real metrics panel, no
    // skeleton) and the chat stays usable.
    expect(screen.getByTestId("metrics-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("skeleton-metrics")).not.toBeInTheDocument();
    expect(screen.getByTestId("slot-chat")).toHaveAttribute(
      "data-chat-disabled",
      "false",
    );
  });
});
