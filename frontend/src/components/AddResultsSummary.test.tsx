/**
 * Tests for {@link AddResultsSummary} (dataset-ingestion task 9.2,
 * Requirements 5.4, 6.5, 6.6).
 *
 * The summary renders one row per result with a friendly, count-free label for
 * each backend outcome, links datasets the analyst can open, and prompts a
 * re-check when any item expired.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AddOutcome } from "../api/ingest";
import { makeAddResult, makeItem } from "../test/fixtures";
import AddResultsSummary from "./AddResultsSummary";

describe("AddResultsSummary", () => {
  it("renders a row for every outcome in the vocabulary", () => {
    const outcomes: AddOutcome[] = [
      "created",
      "refreshed",
      "restored_and_refreshed",
      "already_refreshing",
      "refused_wont_work",
      "needs_confirmation",
      "expired",
    ];
    const results = outcomes.map((outcome, index) =>
      makeAddResult(outcome, { item_id: `u${index}` }),
    );
    const items = outcomes.map((_, index) =>
      makeItem({ item_id: `u${index}`, input: `https://example.com/${index}` }),
    );

    render(<AddResultsSummary results={results} items={items} />);

    const rows = screen.getAllByTestId("add-result-row");
    expect(rows).toHaveLength(outcomes.length);
    for (let i = 0; i < outcomes.length; i += 1) {
      expect(rows[i]).toHaveAttribute("data-outcome", outcomes[i]);
      // Each row shows a non-empty, human-readable outcome label.
      const label = within(rows[i]).getByTestId("add-result-outcome").textContent ?? "";
      expect(label.length).toBeGreaterThan(0);
    }
  });

  it("shows the input URL for each result", () => {
    const results = [makeAddResult("created", { item_id: "u1", dataset_id: "ds-1" })];
    const items = [makeItem({ item_id: "u1", input: "https://example.com/acme" })];

    render(<AddResultsSummary results={results} items={items} />);

    expect(screen.getByTestId("add-result-url")).toHaveTextContent(
      "https://example.com/acme",
    );
    // A navigable result links to the dataset detail page.
    expect(screen.getByTestId("add-result-link")).toHaveAttribute(
      "href",
      "/datasets/ds-1",
    );
  });

  it("prompts a re-check when an item expired (Requirement 5.4)", async () => {
    const user = userEvent.setup();
    const onRecheck = vi.fn();
    const results = [makeAddResult("expired", { item_id: "u1" })];
    const items = [makeItem({ item_id: "u1" })];

    render(
      <AddResultsSummary results={results} items={items} onRecheck={onRecheck} />,
    );

    const expired = screen.getByTestId("add-summary-expired");
    expect(expired).toBeInTheDocument();
    await user.click(screen.getByTestId("add-summary-recheck"));
    expect(onRecheck).toHaveBeenCalledTimes(1);
  });
});
