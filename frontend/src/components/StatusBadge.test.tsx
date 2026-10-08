/**
 * Unit tests for {@link StatusBadge} — the design's `display_state` badge
 * mapping (dataset-library task 6.3, Requirements 2.5, 6.3).
 *
 * Each case asserts the badge text and `data-state`, plus the special cases:
 * the "Checking page…" override while a refresh Check runs, the live progress
 * message on `processing`, and the tooltip (title) carrying the failure reason.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import StatusBadge from "./StatusBadge";

describe("StatusBadge display_state mapping", () => {
  it("renders Processing with the live message", () => {
    render(<StatusBadge displayState="processing" lastMessage="page 4 of 10" />);
    const badge = screen.getByTestId("status-badge");
    expect(badge).toHaveAttribute("data-state", "processing");
    expect(badge).toHaveTextContent("Processing");
    expect(screen.getByTestId("status-message")).toHaveTextContent("page 4 of 10");
  });

  it("renders Ready", () => {
    render(<StatusBadge displayState="ready" />);
    const badge = screen.getByTestId("status-badge");
    expect(badge).toHaveAttribute("data-state", "ready");
    expect(badge).toHaveTextContent("Ready");
  });

  it("renders Ready · refreshing", () => {
    render(<StatusBadge displayState="ready_refreshing" />);
    const badge = screen.getByTestId("status-badge");
    expect(badge).toHaveAttribute("data-state", "ready_refreshing");
    expect(badge).toHaveTextContent("Ready · refreshing");
  });

  it("renders Ready · last refresh failed with the reason as a tooltip", () => {
    render(
      <StatusBadge
        displayState="ready_refresh_failed"
        lastMessage="Refresh failed: 404"
      />,
    );
    const badge = screen.getByTestId("status-badge");
    expect(badge).toHaveAttribute("data-state", "ready_refresh_failed");
    expect(badge).toHaveTextContent("Ready · last refresh failed");
    expect(badge).toHaveAttribute("title", "Refresh failed: 404");
  });

  it("renders Failed with the last event message as a tooltip", () => {
    render(<StatusBadge displayState="failed" lastMessage="No reviews found" />);
    const badge = screen.getByTestId("status-badge");
    expect(badge).toHaveAttribute("data-state", "failed");
    expect(badge).toHaveTextContent("Failed");
    expect(badge).toHaveAttribute("title", "No reviews found");
  });

  it("shows Checking page… while a refresh Check is running (overrides state)", () => {
    render(
      <StatusBadge displayState="ready" refreshChecking lastMessage="ignored" />,
    );
    const badge = screen.getByTestId("status-badge");
    expect(badge).toHaveAttribute("data-state", "checking");
    expect(badge).toHaveTextContent("Checking page…");
  });
});
