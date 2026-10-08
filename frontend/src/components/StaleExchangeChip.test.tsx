/**
 * Tests for {@link StaleExchangeChip} (guardrailed-chat task 6.6,
 * Requirement 9.4) and the stale-Exchange styling in {@link HistoryPane}.
 *
 * Focused coverage for this subtask (the comprehensive ChatPanel tests are task
 * 6.9):
 *   - the chip carries the (older) data version and an explanatory tooltip
 *     wired to it with `aria-describedby`, so it is accessible — Requirement
 *     9.4;
 *   - the version is read from `data-version` and the date via the `<time>`
 *     `dateTime` attribute, never from formatted text (testing.md).
 *
 * The chip-in-context test (a stale Exchange renders `data-stale` plus the chip,
 * and its citation popover still works from the saved snippets) lives in
 * {@link HistoryPane}'s own tests at the integration level; here we unit-test
 * the chip in isolation and verify a stale citation popover works via the shared
 * {@link CitationChip} on a stale Exchange's saved snippet.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { makeCitationSnippet } from "../test/fixtures";
import CitationChip from "./CitationChip";
import StaleExchangeChip from "./StaleExchangeChip";

describe("StaleExchangeChip — label + tooltip (Req 9.4)", () => {
  it("carries the older version and an accessible tooltip", () => {
    render(<StaleExchangeChip version={1} askedAt="2026-09-02T12:00:00Z" />);

    const chip = screen.getByTestId("stale-chip");
    expect(chip).toHaveAttribute("data-version", "1");

    // The tooltip is wired to the chip for assistive tech (aria-describedby),
    // and also mirrored into the native title for pointer users.
    const tooltip = screen.getByTestId("stale-chip-tooltip");
    expect(tooltip).toHaveAttribute("role", "tooltip");
    expect(chip).toHaveAttribute("aria-describedby", tooltip.getAttribute("id") ?? "");
    expect(chip).toHaveAttribute("title");

    // The date is asserted via the machine-readable attribute, not text.
    expect(screen.getByTestId("stale-chip-date")).toHaveAttribute(
      "dateTime",
      "2026-09-02T12:00:00Z",
    );
  });

  it("renders without a date when none is given", () => {
    render(<StaleExchangeChip version={2} askedAt={null} />);

    expect(screen.getByTestId("stale-chip")).toHaveAttribute("data-version", "2");
    expect(screen.queryByTestId("stale-chip-date")).not.toBeInTheDocument();
  });
});

describe("StaleExchangeChip — citation popover still works on a stale Exchange (Req 9.4)", () => {
  it("shows the saved review snippet on a stale citation chip", async () => {
    const user = userEvent.setup();
    // A stale Exchange's citations keep working because they read the snippet
    // saved with the Exchange (not a re-lookup); render that pairing here.
    render(
      <>
        <StaleExchangeChip version={1} askedAt="2026-09-02T12:00:00Z" />
        <CitationChip
          id="r_0012"
          snippet={makeCitationSnippet({ text: "Saved snippet from the old version." })}
        />
      </>,
    );

    await user.hover(screen.getByTestId("citation-chip"));
    expect(screen.getByTestId("citation-popover-text")).toHaveTextContent(
      "Saved snippet from the old version.",
    );
  });
});
