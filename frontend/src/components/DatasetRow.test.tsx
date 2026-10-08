/**
 * Component tests for {@link DatasetRow} — the refresh-Check states on a row
 * (dataset-library task 6.4, Requirements 4.3, 4.4).
 *
 * Focuses on the refresh behaviours the page-level test doesn't exercise:
 * - while a refresh Check runs, the badge shows "Checking page…" and Refresh is
 *   disabled;
 * - a `limited` refresh (`awaiting_confirmation`) surfaces a "Needs
 *   confirmation" button whose dialog lists the verdict reasons and whose
 *   confirm wires through to `onConfirmRefresh`;
 * - a `ready_refresh_failed` row shows the failure reason inline.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Dataset } from "../api/datasets";
import { makeDataset, makeItem, makeVerdict } from "../test/fixtures";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import DatasetRow from "./DatasetRow";

/** Render one row inside a minimal table + query client. */
function renderRow(dataset: Dataset, overrides: Partial<RowCallbacks> = {}) {
  const callbacks: RowCallbacks = {
    onNavigate: vi.fn(),
    onRefresh: vi.fn(),
    onConfirmRefresh: vi.fn(),
    onArchive: vi.fn(),
    onRestore: vi.fn(),
    ...overrides,
  };
  const utils = renderWithClient(
    <table>
      <tbody>
        <DatasetRow dataset={dataset} {...callbacks} />
      </tbody>
    </table>,
  );
  return { ...utils, callbacks };
}

interface RowCallbacks {
  onNavigate: (path: string) => void;
  onRefresh: (id: string) => void;
  onConfirmRefresh: (id: string, checkId: string) => void;
  onArchive: (id: string) => void;
  onRestore: (id: string) => void;
}

beforeEach(() => window.history.pushState(null, "", "/"));
afterEach(() => window.history.pushState(null, "", "/"));

describe("DatasetRow refresh states", () => {
  it("shows Checking page… and disables Refresh while a refresh Check runs (Req 4.4)", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/ingest/checks/chk-r", () =>
        HttpResponse.json(
          {
            check_id: "chk-r",
            created_at: "",
            origin: "refresh",
            items: [makeItem({ item_id: "i1", state: "checking", verdict: null })],
          },
          { status: 200 },
        ),
      ),
    );

    renderRow(makeDataset({ id: "ds-r", refresh_check_id: "chk-r", status: "updated" }));

    const row = await screen.findByTestId("dataset-row");
    expect(within(row).getByTestId("status-badge")).toHaveAttribute("data-state", "checking");

    await user.click(within(row).getByTestId("row-actions-toggle"));
    expect(within(row).getByTestId("action-refresh")).toBeDisabled();
  });

  it("surfaces Needs confirmation for a limited refresh and confirms it (Req 4.3)", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/ingest/checks/chk-l", () =>
        HttpResponse.json(
          {
            check_id: "chk-l",
            created_at: "",
            origin: "refresh",
            items: [
              makeItem({
                item_id: "i1",
                state: "awaiting_confirmation",
                verdict: makeVerdict("limited", {
                  reasons: ["Only 3 reviews visible", "No next-page link"],
                }),
              }),
            ],
          },
          { status: 200 },
        ),
      ),
    );

    const { callbacks } = renderRow(
      makeDataset({ id: "ds-l", refresh_check_id: "chk-l", status: "updated" }),
    );

    const confirmButton = await screen.findByTestId("needs-confirmation-button");
    await user.click(confirmButton);

    const dialog = await screen.findByTestId("refresh-confirm-dialog");
    expect(within(dialog).getAllByTestId("refresh-confirm-reason")).toHaveLength(2);

    await user.click(within(dialog).getByTestId("refresh-confirm-confirm"));
    await waitFor(() =>
      expect(callbacks.onConfirmRefresh).toHaveBeenCalledWith("ds-l", "chk-l"),
    );
  });

  it("shows the failure reason on a last-refresh-failed row (Req 4.2)", async () => {
    renderRow(
      makeDataset({
        id: "ds-f",
        display_state: "ready_refresh_failed",
        last_message: "Refresh failed: 404",
      }),
    );

    const row = await screen.findByTestId("dataset-row");
    expect(within(row).getByTestId("refresh-failed-reason")).toHaveTextContent(
      "Refresh failed: 404",
    );
  });
});
