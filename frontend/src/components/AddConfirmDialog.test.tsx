/**
 * Tests for {@link AddConfirmDialog} (dataset-ingestion task 9.2,
 * Requirement 3.9).
 *
 * The dialog lists how each selected item will be handled (new vs refresh),
 * calls out the `limited` items with their reasons, and reports the limited
 * item ids on confirm so the container can send `confirm_limited: true`.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { makeExistingDataset, makeItem, makeVerdict } from "../test/fixtures";
import AddConfirmDialog from "./AddConfirmDialog";

describe("AddConfirmDialog", () => {
  it("labels each item as new or refresh and lists limited reasons", () => {
    const items = [
      makeItem({
        item_id: "u1",
        input: "https://example.com/new",
        verdict: makeVerdict("will_work"),
        existing_dataset: null,
      }),
      makeItem({
        item_id: "u2",
        input: "https://example.com/tracked",
        verdict: makeVerdict("limited", { reasons: ["Only 2 reviews found"] }),
        existing_dataset: makeExistingDataset({ name: "Acme CRM" }),
      }),
    ];

    render(<AddConfirmDialog items={items} onConfirm={vi.fn()} onCancel={vi.fn()} />);

    const rows = screen.getAllByTestId("add-confirm-item");
    expect(rows[0]).toHaveAttribute("data-handling", "new");
    expect(rows[1]).toHaveAttribute("data-handling", "refresh");

    // The limited item is called out with its reason (Requirement 3.9).
    const limited = screen.getByTestId("add-confirm-limited");
    expect(within(limited).getByTestId("add-confirm-limited-reason")).toHaveTextContent(
      "Only 2 reviews found",
    );
  });

  it("reports only the limited item ids on confirm", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const items = [
      makeItem({ item_id: "u1", verdict: makeVerdict("will_work") }),
      makeItem({ item_id: "u2", verdict: makeVerdict("limited") }),
    ];

    render(<AddConfirmDialog items={items} onConfirm={onConfirm} onCancel={vi.fn()} />);

    await user.click(screen.getByTestId("add-confirm-confirm"));
    expect(onConfirm).toHaveBeenCalledWith(["u2"]);
  });

  it("cancels without confirming", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    const items = [makeItem({ item_id: "u1", verdict: makeVerdict("limited") })];

    render(<AddConfirmDialog items={items} onConfirm={onConfirm} onCancel={onCancel} />);

    await user.click(screen.getByTestId("add-confirm-cancel"));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
