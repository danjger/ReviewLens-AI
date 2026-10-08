/**
 * Tests for {@link VersionNote} (guardrailed-chat task 6.3, Requirement 1.1).
 *
 * The note tells the analyst which version answers come from while a refresh is
 * running or after one failed, and renders nothing otherwise. Tests assert the
 * state and the active version via `data-*` (testing.md: never match the
 * formatted string / version number as text).
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import VersionNote from "./VersionNote";

describe("VersionNote", () => {
  it("renders nothing in the normal (none) state", () => {
    const { container } = render(<VersionNote activeVersion={3} state="none" />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId("version-note")).toBeNull();
  });

  it("shows the 'until the refresh finishes' note while refreshing", () => {
    render(<VersionNote activeVersion={2} state="refreshing" />);
    const note = screen.getByTestId("version-note");
    expect(note).toHaveAttribute("data-state", "refreshing");
    expect(note).toHaveAttribute("data-active-version", "2");
    expect(note.textContent ?? "").toMatch(/until the refresh finishes/i);
  });

  it("shows the 'last refresh failed' note after a failed refresh", () => {
    render(<VersionNote activeVersion={5} state="refresh_failed" />);
    const note = screen.getByTestId("version-note");
    expect(note).toHaveAttribute("data-state", "refresh_failed");
    expect(note).toHaveAttribute("data-active-version", "5");
    expect(note.textContent ?? "").toMatch(/last refresh failed/i);
  });
});
