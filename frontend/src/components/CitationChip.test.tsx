/**
 * Tests for {@link CitationChip} and {@link DeclineTag} (guardrailed-chat task
 * 6.2, Requirements 2.2, 3.4).
 *
 * Focused coverage for this subtask (the comprehensive ChatPanel tests are task
 * 6.9):
 *   - the citation chip reveals its saved review text/rating/date on hover
 *     *and* on keyboard focus, and hides again on leave/blur/Escape (so the
 *     popover is keyboard-accessible, not hover-only) — Requirement 2.2;
 *   - a missing snippet renders a muted, non-interactive chip with no popover
 *     (defensive) — Requirement 2.2;
 *   - the decline tag renders the subtle "Outside dataset scope" label when an
 *     answer was declined — Requirement 3.4.
 *
 * The popover shows the review *text* (literal page text, which the chip
 * renders verbatim) — that is review text, not a count or date, so asserting on
 * it is allowed; the tests never assert on formatted dates or counts
 * (testing.md).
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { makeCitationSnippet } from "../test/fixtures";
import CitationChip from "./CitationChip";
import DeclineTag from "./DeclineTag";

describe("CitationChip — popover (Req 2.2)", () => {
  it("renders the chip collapsed, with the id visible and no popover", () => {
    render(<CitationChip id="r_0012" snippet={makeCitationSnippet()} />);

    const chip = screen.getByTestId("citation-chip");
    expect(chip).toHaveAttribute("data-citation-id", "r_0012");
    expect(chip).toHaveAttribute("data-has-snippet", "true");
    expect(screen.getByTestId("citation-chip-button")).toHaveTextContent("r_0012");
    expect(screen.getByTestId("citation-chip-button")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("citation-popover")).not.toBeInTheDocument();
  });

  it("shows the saved review text on hover and hides it on leave", async () => {
    const user = userEvent.setup();
    render(
      <CitationChip
        id="r_0012"
        snippet={makeCitationSnippet({ text: "Support took five days to reply." })}
      />,
    );

    await user.hover(screen.getByTestId("citation-chip"));
    const popover = screen.getByTestId("citation-popover");
    expect(screen.getByTestId("citation-popover-text")).toHaveTextContent(
      "Support took five days to reply.",
    );
    // The popover is linked to the chip for assistive tech.
    expect(popover).toHaveAttribute("role", "tooltip");
    expect(screen.getByTestId("citation-chip-button")).toHaveAttribute(
      "aria-describedby",
      popover.getAttribute("id") ?? "",
    );

    await user.unhover(screen.getByTestId("citation-chip"));
    expect(screen.queryByTestId("citation-popover")).not.toBeInTheDocument();
  });

  it("shows the popover on keyboard focus (not hover-only) and hides on blur", async () => {
    const user = userEvent.setup();
    render(
      <>
        <CitationChip
          id="r_0012"
          snippet={makeCitationSnippet({ text: "Great onboarding experience." })}
        />
        <button type="button">next</button>
      </>,
    );

    // Tab to the chip button — the popover appears on focus alone.
    await user.tab();
    expect(screen.getByTestId("citation-chip-button")).toHaveFocus();
    expect(screen.getByTestId("citation-popover-text")).toHaveTextContent(
      "Great onboarding experience.",
    );
    expect(screen.getByTestId("citation-chip-button")).toHaveAttribute("aria-expanded", "true");

    // Tab away — blur hides the popover.
    await user.tab();
    expect(screen.queryByTestId("citation-popover")).not.toBeInTheDocument();
  });

  it("dismisses the popover on Escape", async () => {
    const user = userEvent.setup();
    render(<CitationChip id="r_0012" snippet={makeCitationSnippet()} />);

    await user.tab();
    expect(screen.getByTestId("citation-popover")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("citation-popover")).not.toBeInTheDocument();
  });

  it("shows the rating and date in the popover when present", async () => {
    const user = userEvent.setup();
    render(
      <CitationChip id="r_0012" snippet={makeCitationSnippet({ rating: 2, date: "2026-08-14" })} />,
    );

    await user.hover(screen.getByTestId("citation-chip"));
    expect(screen.getByTestId("citation-popover-rating")).toBeInTheDocument();
    expect(screen.getByTestId("citation-popover-date")).toBeInTheDocument();
  });

  it("omits the rating/date line when the snippet has neither", async () => {
    const user = userEvent.setup();
    render(
      <CitationChip
        id="r_0012"
        snippet={makeCitationSnippet({ text: "No meta here.", rating: null, date: null })}
      />,
    );

    await user.hover(screen.getByTestId("citation-chip"));
    expect(screen.getByTestId("citation-popover-text")).toHaveTextContent("No meta here.");
    expect(screen.queryByTestId("citation-popover-rating")).not.toBeInTheDocument();
    expect(screen.queryByTestId("citation-popover-date")).not.toBeInTheDocument();
  });
});

describe("CitationChip — missing snippet (defensive, Req 2.2)", () => {
  it.each([
    ["null", null],
    ["undefined", undefined],
  ])("renders a muted, non-interactive chip with no popover when the snippet is %s", async (
    _label,
    snippet,
  ) => {
    const user = userEvent.setup();
    render(<CitationChip id="r_9999" snippet={snippet} />);

    const chip = screen.getByTestId("citation-chip");
    expect(chip).toHaveAttribute("data-has-snippet", "false");
    expect(chip).toHaveTextContent("r_9999");
    // No interactive button and no popover, even on hover.
    expect(screen.queryByTestId("citation-chip-button")).not.toBeInTheDocument();
    await user.hover(chip);
    expect(screen.queryByTestId("citation-popover")).not.toBeInTheDocument();
  });
});

describe("DeclineTag — subtle scope tag (Req 3.4)", () => {
  it("renders the 'Outside dataset scope' label", () => {
    render(<DeclineTag />);
    const tag = screen.getByTestId("decline-tag");
    expect(tag).toHaveTextContent("Outside dataset scope");
  });

  it("carries the decline category as a data attribute when known", () => {
    render(<DeclineTag category="world_knowledge" />);
    expect(screen.getByTestId("decline-tag")).toHaveAttribute(
      "data-scope-category",
      "world_knowledge",
    );
  });
});
