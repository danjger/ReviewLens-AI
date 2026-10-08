/**
 * Integration tests for {@link AddPanel} wired through MSW (dataset-ingestion
 * task 9.2).
 *
 * Covers the key Add behaviors:
 * - a selection with a `limited` item opens the confirmation and the submitted
 *   payload carries `confirm_limited: true` for that item (Requirement 3.9);
 * - a selection of only `will_work` items is added with no confirmation;
 * - a single navigable result navigates to the dataset detail page, while
 *   several results stay and render the summary (Requirement 5.4).
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AddItemInput } from "../api/ingest";
import { makeExistingDataset, makeItem, makeVerdict } from "../test/fixtures";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import AddPanel from "./AddPanel";

/** Capture the body an Add request was called with. */
function stubAdd(response: {
  results: Array<{
    item_id: string;
    outcome: string;
    dataset_id: string | null;
    message: string | null;
  }>;
}): { body: () => { items: AddItemInput[] } | null } {
  let captured: { items: AddItemInput[] } | null = null;
  server.use(
    http.post("/api/ingest/checks/chk-1/add", async ({ request }) => {
      captured = (await request.json()) as { items: AddItemInput[] };
      return HttpResponse.json(response, { status: 200 });
    }),
  );
  return { body: () => captured };
}

beforeEach(() => window.history.pushState(null, "", "/"));
afterEach(() => window.history.pushState(null, "", "/"));

describe("AddPanel", () => {
  it("confirms a limited selection and sends confirm_limited (Requirement 3.9)", async () => {
    const user = userEvent.setup();
    const captured = stubAdd({
      results: [{ item_id: "u2", outcome: "created", dataset_id: "ds-2", message: null }],
    });

    const items = [
      makeItem({ item_id: "u1", verdict: makeVerdict("will_work") }),
      makeItem({ item_id: "u2", verdict: makeVerdict("limited") }),
    ];

    renderWithClient(
      <AddPanel
        checkId="chk-1"
        items={items}
        includedItemIds={["u1", "u2"]}
        navigation={{ onNavigate: vi.fn() }}
      />,
    );

    await user.click(screen.getByTestId("add-selected-button"));

    // The confirmation appears because a limited item is selected.
    const dialog = await screen.findByTestId("add-confirm-dialog");
    await user.click(within(dialog).getByTestId("add-confirm-confirm"));

    await waitFor(() => expect(captured.body()).not.toBeNull());
    const sent = captured.body() as { items: AddItemInput[] };
    const byId = Object.fromEntries(sent.items.map((i) => [i.item_id, i]));
    expect(byId.u1.confirm_limited).toBeUndefined();
    expect(byId.u2.confirm_limited).toBe(true);
  });

  it("adds a will_work-only selection without a confirmation", async () => {
    const user = userEvent.setup();
    stubAdd({
      results: [{ item_id: "u1", outcome: "created", dataset_id: "ds-1", message: null }],
    });
    const onNavigate = vi.fn();

    const items = [makeItem({ item_id: "u1", verdict: makeVerdict("will_work") })];

    renderWithClient(
      <AddPanel
        checkId="chk-1"
        items={items}
        includedItemIds={["u1"]}
        navigation={{ onNavigate }}
      />,
    );

    await user.click(screen.getByTestId("add-selected-button"));

    // No dialog; it navigates straight to the single created dataset.
    expect(screen.queryByTestId("add-confirm-dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(onNavigate).toHaveBeenCalledWith("/datasets/ds-1"));
  });

  it("navigates to the detail page for a single add (Requirement 5.4)", async () => {
    const user = userEvent.setup();
    stubAdd({
      results: [
        { item_id: "u1", outcome: "refreshed", dataset_id: "ds-7", message: null },
      ],
    });
    const onNavigate = vi.fn();

    const items = [
      makeItem({
        item_id: "u1",
        verdict: makeVerdict("will_work"),
        existing_dataset: makeExistingDataset({ id: "ds-7" }),
      }),
    ];

    renderWithClient(
      <AddPanel
        checkId="chk-1"
        items={items}
        includedItemIds={["u1"]}
        navigation={{ onNavigate }}
      />,
    );

    await user.click(screen.getByTestId("add-selected-button"));
    await waitFor(() => expect(onNavigate).toHaveBeenCalledWith("/datasets/ds-7"));
    // A single-add navigation does not render the in-place summary.
    expect(screen.queryByTestId("add-results-summary")).not.toBeInTheDocument();
  });

  it("stays and shows the summary for a multi add (Requirement 5.4)", async () => {
    const user = userEvent.setup();
    stubAdd({
      results: [
        { item_id: "u1", outcome: "created", dataset_id: "ds-1", message: null },
        { item_id: "u2", outcome: "refreshed", dataset_id: "ds-2", message: null },
      ],
    });
    const onNavigate = vi.fn();

    const items = [
      makeItem({ item_id: "u1", verdict: makeVerdict("will_work") }),
      makeItem({
        item_id: "u2",
        verdict: makeVerdict("will_work"),
        existing_dataset: makeExistingDataset({ id: "ds-2" }),
      }),
    ];

    renderWithClient(
      <AddPanel
        checkId="chk-1"
        items={items}
        includedItemIds={["u1", "u2"]}
        navigation={{ onNavigate }}
      />,
    );

    await user.click(screen.getByTestId("add-selected-button"));

    // Several results → stay here with the summary, no navigation.
    const summary = await screen.findByTestId("add-results-summary");
    expect(within(summary).getAllByTestId("add-result-row")).toHaveLength(2);
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("cancelling the limited confirmation adds nothing (Requirement 3.9)", async () => {
    const user = userEvent.setup();
    let addCalled = false;
    server.use(
      http.post("/api/ingest/checks/chk-1/add", () => {
        addCalled = true;
        return HttpResponse.json({ results: [] }, { status: 200 });
      }),
    );

    const items = [makeItem({ item_id: "u1", verdict: makeVerdict("limited") })];

    renderWithClient(
      <AddPanel
        checkId="chk-1"
        items={items}
        includedItemIds={["u1"]}
        navigation={{ onNavigate: vi.fn() }}
      />,
    );

    await user.click(screen.getByTestId("add-selected-button"));
    const dialog = await screen.findByTestId("add-confirm-dialog");
    await user.click(within(dialog).getByTestId("add-confirm-cancel"));

    // The dialog closes and no Add request is sent.
    await waitFor(() =>
      expect(screen.queryByTestId("add-confirm-dialog")).not.toBeInTheDocument(),
    );
    expect(addCalled).toBe(false);
  });

  it("never sends a stale wont_work item even if it lingers in the selection (Requirement 3.8)", async () => {
    const user = userEvent.setup();
    const captured = stubAdd({
      results: [{ item_id: "u1", outcome: "created", dataset_id: "ds-1", message: null }],
    });

    const items = [
      makeItem({ item_id: "u1", verdict: makeVerdict("will_work") }),
      makeItem({ item_id: "u2", verdict: makeVerdict("wont_work", { reasons: ["Not found (404)"] }) }),
    ];

    renderWithClient(
      <AddPanel
        checkId="chk-1"
        items={items}
        // u2 is wont_work but still present in the selection ids.
        includedItemIds={["u1", "u2"]}
        navigation={{ onNavigate: vi.fn() }}
      />,
    );

    await user.click(screen.getByTestId("add-selected-button"));

    await waitFor(() => expect(captured.body()).not.toBeNull());
    const sent = captured.body() as { items: AddItemInput[] };
    const ids = sent.items.map((i) => i.item_id);
    expect(ids).toContain("u1");
    expect(ids).not.toContain("u2");
  });

  it("disables Add when the selection has no addable item", () => {
    const items = [
      makeItem({ item_id: "u1", verdict: makeVerdict("wont_work", { reasons: ["Not found (404)"] }) }),
    ];
    renderWithClient(
      <AddPanel
        checkId="chk-1"
        items={items}
        includedItemIds={["u1"]}
        navigation={{ onNavigate: vi.fn() }}
      />,
    );
    expect(screen.getByTestId("add-selected-button")).toBeDisabled();
  });

  it("shows the resume message when Add is rate-limited (Requirement 1.6)", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/ingest/checks/chk-1/add", () =>
        HttpResponse.json(
          { error: { code: "RATE_LIMITED", message: "Too many requests." } },
          { status: 429, headers: { "Retry-After": "30" } },
        ),
      ),
    );

    const items = [makeItem({ item_id: "u1", verdict: makeVerdict("will_work") })];
    renderWithClient(
      <AddPanel
        checkId="chk-1"
        items={items}
        includedItemIds={["u1"]}
        navigation={{ onNavigate: vi.fn() }}
      />,
    );

    await user.click(screen.getByTestId("add-selected-button"));
    expect(await screen.findByTestId("rate-limit-message")).toHaveTextContent("30 seconds");
  });

  it("renders the expired summary and wires a re-check (Requirement 5.4)", async () => {
    const user = userEvent.setup();
    const onRecheck = vi.fn();
    stubAdd({
      results: [{ item_id: "u1", outcome: "expired", dataset_id: null, message: null }],
    });

    const items = [makeItem({ item_id: "u1", verdict: makeVerdict("will_work") })];
    renderWithClient(
      <AddPanel
        checkId="chk-1"
        items={items}
        includedItemIds={["u1"]}
        onRecheck={onRecheck}
        navigation={{ onNavigate: vi.fn() }}
      />,
    );

    await user.click(screen.getByTestId("add-selected-button"));
    const expired = await screen.findByTestId("add-summary-expired");
    expect(expired).toBeInTheDocument();
    await user.click(screen.getByTestId("add-summary-recheck"));
    expect(onRecheck).toHaveBeenCalledTimes(1);
  });
});
