/**
 * Tests for {@link CheckResultsList} (dataset-ingestion task 9.4).
 *
 * The list renders one {@link VerdictCard} per Check item and threads the
 * selection map and the toggle/retry callbacks straight through. These cases
 * cover the layout contract: nothing is rendered for an empty check, a card is
 * rendered per item, the per-item `included` state comes from the selection map,
 * and the retry/toggle callbacks reach the parent with the right item id.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { makeItem, makeVerdict } from "../test/fixtures";
import CheckResultsList from "./CheckResultsList";

describe("CheckResultsList", () => {
  it("renders nothing when there are no items", () => {
    const { container } = render(
      <CheckResultsList
        items={[]}
        selection={{}}
        onToggleInclude={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId("check-results")).not.toBeInTheDocument();
  });

  it("renders one card per item", () => {
    const items = [
      makeItem({ item_id: "u1" }),
      makeItem({ item_id: "u2" }),
      makeItem({ item_id: "u3" }),
    ];
    render(
      <CheckResultsList
        items={items}
        selection={{}}
        onToggleInclude={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getAllByTestId("verdict-card")).toHaveLength(3);
  });

  it("reflects the per-item selection map in each card's checkbox", () => {
    const items = [
      makeItem({ item_id: "u1" }),
      makeItem({ item_id: "u2" }),
    ];
    render(
      <CheckResultsList
        items={items}
        selection={{ u1: true, u2: false }}
        onToggleInclude={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    const cards = screen.getAllByTestId("verdict-card");
    expect(within(cards[0]).getByTestId("include-checkbox")).toBeChecked();
    expect(within(cards[1]).getByTestId("include-checkbox")).not.toBeChecked();
  });

  it("passes toggle changes up with the item id", () => {
    const onToggleInclude = vi.fn();
    render(
      <CheckResultsList
        items={[makeItem({ item_id: "u1" })]}
        selection={{ u1: false }}
        onToggleInclude={onToggleInclude}
        onRetry={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("include-checkbox"));
    expect(onToggleInclude).toHaveBeenCalledWith("u1", true);
  });

  it("passes retry clicks up with the item id", () => {
    const onRetry = vi.fn();
    render(
      <CheckResultsList
        items={[makeItem({ item_id: "u2", state: "error", verdict: null })]}
        selection={{}}
        onToggleInclude={vi.fn()}
        onRetry={onRetry}
      />,
    );
    fireEvent.click(screen.getByTestId("retry-link"));
    expect(onRetry).toHaveBeenCalledWith("u2");
  });

  it("disables every card's retry while a retry is in flight", () => {
    const items = [
      makeItem({ item_id: "u1", state: "error", verdict: null }),
      makeItem({
        item_id: "u2",
        verdict: makeVerdict("wont_work", { reasons: ["Page took too long to load"] }),
      }),
    ];
    render(
      <CheckResultsList
        items={items}
        selection={{}}
        onToggleInclude={vi.fn()}
        onRetry={vi.fn()}
        retrying
      />,
    );
    for (const link of screen.getAllByTestId("retry-link")) {
      expect(link).toBeDisabled();
    }
  });
});
