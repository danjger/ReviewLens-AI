/**
 * Focused tests for {@link HistoryToolbar} (guardrailed-chat task 6.7,
 * Requirement 9.5).
 *
 * Covers the presentational contract: jump buttons invoke their callbacks and
 * are disabled when there are no markers, and the collapse toggle is a real
 * button with `aria-expanded`/`aria-pressed` that flips through its callback
 * and is disabled when there is nothing to fold. Selectors are data-testid /
 * data-* only (testing.md). Comprehensive coverage is task 6.9.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import HistoryToolbar from "./HistoryToolbar";

function renderToolbar(overrides: Partial<Parameters<typeof HistoryToolbar>[0]> = {}) {
  const props = {
    onJumpPrev: vi.fn(),
    onJumpNext: vi.fn(),
    hasMarkers: true,
    collapsed: false,
    onToggleCollapse: vi.fn(),
    canCollapse: true,
    ...overrides,
  };
  render(<HistoryToolbar {...props} />);
  return props;
}

describe("HistoryToolbar — jump buttons (Req 9.5)", () => {
  it("invokes the prev/next callbacks on click", async () => {
    const user = userEvent.setup();
    const props = renderToolbar();

    await user.click(screen.getByTestId("history-jump-prev"));
    await user.click(screen.getByTestId("history-jump-next"));

    expect(props.onJumpPrev).toHaveBeenCalledTimes(1);
    expect(props.onJumpNext).toHaveBeenCalledTimes(1);
  });

  it("disables both jump buttons when there are no markers", () => {
    renderToolbar({ hasMarkers: false });
    expect(screen.getByTestId("history-jump-prev")).toBeDisabled();
    expect(screen.getByTestId("history-jump-next")).toBeDisabled();
  });

  it("gives the jump buttons accessible labels", () => {
    renderToolbar();
    expect(screen.getByTestId("history-jump-prev")).toHaveAccessibleName(
      "Jump to previous refresh",
    );
    expect(screen.getByTestId("history-jump-next")).toHaveAccessibleName(
      "Jump to next refresh",
    );
  });
});

describe("HistoryToolbar — collapse toggle (Req 9.5)", () => {
  it("is a button with aria-expanded reflecting the collapse state", () => {
    renderToolbar({ collapsed: false });
    const toggle = screen.getByTestId("history-collapse-toggle");
    expect(toggle.tagName).toBe("BUTTON");
    // Not collapsed → earlier versions are expanded.
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(toggle).toHaveAttribute("data-collapsed", "false");
  });

  it("reflects the collapsed state via aria-expanded/aria-pressed/data-collapsed", () => {
    renderToolbar({ collapsed: true });
    const toggle = screen.getByTestId("history-collapse-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(toggle).toHaveAttribute("data-collapsed", "true");
  });

  it("invokes onToggleCollapse on click", async () => {
    const user = userEvent.setup();
    const props = renderToolbar();
    await user.click(screen.getByTestId("history-collapse-toggle"));
    expect(props.onToggleCollapse).toHaveBeenCalledTimes(1);
  });

  it("disables the toggle when there are no earlier versions to fold", () => {
    renderToolbar({ canCollapse: false });
    expect(screen.getByTestId("history-collapse-toggle")).toBeDisabled();
  });
});
