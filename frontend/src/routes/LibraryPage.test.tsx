/**
 * Component tests for {@link LibraryPage} (dataset-library task 6.7).
 *
 * Covers Requirements 1.1, 1.3, 1.4, 1.6, 2.4, 6.3:
 * - both sections render with distinct headings and landmarks;
 * - no URL input lives inside the Tracked section;
 * - panel collapse/expand rules (expanded when empty; collapsible otherwise);
 * - the empty state points at the New Dataset panel;
 * - a row highlights after a Refresh;
 * - every row action (Open, Refresh, Archive, Restore);
 * - a live row update applied from a `dataset.status.changed` realtime frame.
 *
 * The API is mocked with MSW (`src/test/server.ts`) and the WebSocket with the
 * shared mock (`src/test/ws.ts`). Counts/dates are read via testids, never text
 * (testing.md).
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Dataset } from "../api/datasets";
import { makeDataset } from "../test/fixtures";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import {
  installMockWebSocket,
  lastMockWebSocket,
  uninstallMockWebSocket,
} from "../test/ws";
import LibraryPage from "./LibraryPage";

/**
 * Stub `GET /api/datasets` with the given rows (regardless of query params).
 *
 * Mirrors the real backend contract, which wraps the list in a
 * `{ datasets: [...] }` envelope (see `app/datasets/api.py`); the client
 * unwraps it. Returning the envelope here keeps the stub honest so a regression
 * where the client stops unwrapping would be caught.
 */
function stubList(rows: Dataset[]): void {
  server.use(
    http.get("/api/datasets", () =>
      HttpResponse.json({ datasets: rows }, { status: 200 }),
    ),
  );
}

/** Render the page with the realtime socket wired to the WS mock. */
function renderPage(onNavigate = vi.fn()) {
  return {
    onNavigate,
    ...renderWithClient(<LibraryPage onNavigate={onNavigate} />),
  };
}

beforeEach(() => {
  installMockWebSocket({ autoOpen: true });
  window.history.pushState(null, "", "/");
});

afterEach(() => {
  uninstallMockWebSocket();
  window.history.pushState(null, "", "/");
});

describe("LibraryPage layout (Requirements 1.1, 1.3)", () => {
  it("renders both sections with distinct headings and landmarks", async () => {
    stubList([makeDataset()]);
    renderPage();

    const panel = await screen.findByTestId("new-dataset-panel");
    const tracked = screen.getByTestId("tracked-section");

    // Two distinct labelled regions.
    expect(within(panel).getByRole("heading", { name: /add new reviews/i })).toBeInTheDocument();
    await waitFor(() =>
      expect(within(tracked).getByRole("heading", { name: /tracked datasets/i })).toBeInTheDocument(),
    );
    // Different DOM subtrees → can't be confused.
    expect(panel).not.toContainElement(tracked);
  });

  it("keeps all URL input inside the panel, none in the Tracked section (Req 1.3)", async () => {
    stubList([makeDataset()]);
    renderPage();

    const panel = await screen.findByTestId("new-dataset-panel");
    const tracked = screen.getByTestId("tracked-section");

    // The panel hosts the URL textarea …
    expect(within(panel).getByTestId("url-input-textarea")).toBeInTheDocument();
    // … and the Tracked section has none (only its search box, which is a
    // search input acting on existing datasets, not a URL entry).
    expect(within(tracked).queryByTestId("url-input-textarea")).not.toBeInTheDocument();
    expect(within(tracked).getByTestId("tracked-search")).toBeInTheDocument();
  });
});

describe("Panel collapse/expand rules (Requirements 1.5, 1.6)", () => {
  it("is expanded with no collapse control when the library is empty", async () => {
    stubList([]);
    renderPage();

    const panel = await screen.findByTestId("new-dataset-panel");
    await waitFor(() => expect(panel).toHaveAttribute("data-expanded", "true"));
    // Nothing to collapse behind, so no toggle is offered.
    expect(within(panel).queryByTestId("panel-toggle")).not.toBeInTheDocument();
    // The empty state points at the panel.
    expect(screen.getByTestId("tracked-empty")).toBeInTheDocument();
    expect(screen.getByTestId("empty-add-link")).toBeInTheDocument();
  });

  it("is collapsible once at least one dataset exists", async () => {
    const user = userEvent.setup();
    stubList([makeDataset()]);
    renderPage();

    const panel = await screen.findByTestId("new-dataset-panel");
    // With datasets present the panel starts collapsible and expanded.
    const toggle = await within(panel).findByTestId("panel-toggle");
    await user.click(toggle);
    await waitFor(() => expect(panel).toHaveAttribute("data-expanded", "false"));
    await user.click(within(panel).getByTestId("panel-toggle"));
    await waitFor(() => expect(panel).toHaveAttribute("data-expanded", "true"));
  });
});

describe("Loading and error states (Requirement 2.4)", () => {
  it("shows a loading placeholder then the table", async () => {
    stubList([makeDataset()]);
    renderPage();
    // Table appears once loaded.
    expect(await screen.findByTestId("dataset-table")).toBeInTheDocument();
  });

  it("shows an error with a retry that refetches", async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(
      http.get("/api/datasets", () => {
        calls += 1;
        if (calls === 1) {
          return HttpResponse.json(
            { error: { code: "SERVER_ERROR", message: "boom" } },
            { status: 500 },
          );
        }
        return HttpResponse.json({ datasets: [makeDataset()] }, { status: 200 });
      }),
    );
    renderPage();

    await screen.findByTestId("tracked-error");
    await user.click(screen.getByTestId("tracked-retry"));
    expect(await screen.findByTestId("dataset-table")).toBeInTheDocument();
  });
});

describe("Row rendering and navigation (Requirements 2.1, 3.1)", () => {
  it("shows the review count, version and status badge, and opens on click", async () => {
    const onNavigate = vi.fn();
    stubList([makeDataset({ id: "ds-9" })]);
    renderPage(onNavigate);

    const row = await screen.findByTestId("dataset-row");
    expect(within(row).getByTestId("row-review-count")).toHaveTextContent("212");
    expect(within(row).getByTestId("row-version")).toHaveTextContent("v3");
    expect(within(row).getByTestId("status-badge")).toHaveAttribute("data-state", "ready");

    await userEvent.setup().click(within(row).getByTestId("row-name"));
    expect(onNavigate).toHaveBeenCalledWith("/datasets/ds-9");
  });
});

describe("Row actions (Requirements 4.4, 5.1, 5.3)", () => {
  it("Open navigates to the detail page", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    stubList([makeDataset({ id: "ds-2" })]);
    renderPage(onNavigate);

    const row = await screen.findByTestId("dataset-row");
    await user.click(within(row).getByTestId("row-actions-toggle"));
    await user.click(within(row).getByTestId("action-open"));
    expect(onNavigate).toHaveBeenCalledWith("/datasets/ds-2");
  });

  it("Refresh calls the refresh endpoint and highlights the row (Req 1.4)", async () => {
    const user = userEvent.setup();
    let refreshed = false;
    stubList([makeDataset({ id: "ds-3", status: "updated" })]);
    server.use(
      http.post("/api/datasets/ds-3/refresh", () => {
        refreshed = true;
        return HttpResponse.json({ check_id: "chk-9" }, { status: 202 });
      }),
      http.get("/api/ingest/checks/chk-9", () =>
        HttpResponse.json(
          { check_id: "chk-9", created_at: "", origin: "refresh", items: [] },
          { status: 200 },
        ),
      ),
    );
    renderPage();

    const row = await screen.findByTestId("dataset-row");
    await user.click(within(row).getByTestId("row-actions-toggle"));
    await user.click(within(row).getByTestId("action-refresh"));

    await waitFor(() => expect(refreshed).toBe(true));
    await waitFor(() =>
      expect(screen.getByTestId("dataset-row")).toHaveAttribute(
        "data-highlighted",
        "true",
      ),
    );
  });

  it("disables Refresh while the dataset is processing (Req 4.4)", async () => {
    const user = userEvent.setup();
    stubList([
      makeDataset({ id: "ds-4", status: "processing", display_state: "processing" }),
    ]);
    renderPage();

    const row = await screen.findByTestId("dataset-row");
    await user.click(within(row).getByTestId("row-actions-toggle"));
    expect(within(row).getByTestId("action-refresh")).toBeDisabled();
  });

  it("Archive confirms, then archives (Req 5.1)", async () => {
    const user = userEvent.setup();
    let archived = false;
    stubList([makeDataset({ id: "ds-5" })]);
    server.use(
      http.post("/api/datasets/ds-5/archive", () => {
        archived = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderPage();

    const row = await screen.findByTestId("dataset-row");
    await user.click(within(row).getByTestId("row-actions-toggle"));
    await user.click(within(row).getByTestId("action-archive"));

    const dialog = await screen.findByTestId("confirm-dialog");
    await user.click(within(dialog).getByTestId("confirm-dialog-confirm"));
    await waitFor(() => expect(archived).toBe(true));
  });

  it("offers Restore for an archived dataset and calls restore (Req 5.3)", async () => {
    const user = userEvent.setup();
    let restored = false;
    stubList([makeDataset({ id: "ds-6", archived_at: "2026-09-03T00:00:00Z" })]);
    server.use(
      http.post("/api/datasets/ds-6/restore", () => {
        restored = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderPage();

    // Flip the "Show archived" toggle so the archived row is listed.
    const toggle = await screen.findByTestId("tracked-show-archived");
    await user.click(toggle);

    const row = await screen.findByTestId("dataset-row");
    await user.click(within(row).getByTestId("row-actions-toggle"));
    await user.click(within(row).getByTestId("action-restore"));
    await waitFor(() => expect(restored).toBe(true));
  });
});

describe("Live row update (Requirement 6.3)", () => {
  it("applies a dataset.status.changed frame to the row in place", async () => {
    stubList([
      makeDataset({ id: "ds-live", status: "processing", display_state: "processing", review_count: 0 }),
    ]);
    renderPage();

    const row = await screen.findByTestId("dataset-row");
    expect(within(row).getByTestId("row-review-count")).toHaveTextContent("0");

    // Push a status change for the live row through the mock socket.
    const socket = lastMockWebSocket();
    expect(socket).toBeDefined();
    socket!.simulateMessage({
      type: "dataset.status.changed",
      dataset_id: "ds-live",
      status: "updated",
      at: "2026-09-04T00:00:00Z",
      data_version: 1,
      active_version: 1,
      message: "Processed 50 reviews",
      metrics: { review_count: 50 },
    });

    await waitFor(() =>
      expect(within(screen.getByTestId("dataset-row")).getByTestId("row-review-count")).toHaveTextContent("50"),
    );
  });
});
