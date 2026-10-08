/**
 * Tests for {@link RefreshMarker} (guardrailed-chat task 6.6, Requirements
 * 9.1, 9.2, 9.3).
 *
 * Focused per-state render coverage (the comprehensive ChatPanel tests and the
 * live pending→completed change are task 6.9):
 *   - completed: version, trigger wording, and before/after counts — asserted
 *     via `data-*` attributes, never the formatted count/date text (testing.md)
 *     — Requirement 9.1;
 *   - pending: a spinner with an accessible label and the pending state —
 *     Requirement 9.2;
 *   - failed: the failed state and the prior version in the copy —
 *     Requirement 9.3.
 *
 * Every value a test needs (version, state, trigger, counts) is read from the
 * `data-*` attributes; the completed datetime is asserted via the `<time>`
 * `dateTime` attribute, so no assertion depends on a formatted count or date.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeRefreshMarker } from "../test/fixtures";
import RefreshMarker from "./RefreshMarker";

describe("RefreshMarker — completed (Req 9.1)", () => {
  it("exposes version, trigger, and before/after counts as data-* attributes", () => {
    render(
      <RefreshMarker
        marker={makeRefreshMarker({
          version: 3,
          state: "completed",
          trigger: "manual_refresh",
          review_count: 212,
          previous_review_count: 180,
        })}
      />,
    );

    const marker = screen.getByTestId("refresh-marker");
    expect(marker).toHaveAttribute("data-state", "completed");
    expect(marker).toHaveAttribute("data-version", "3");
    expect(marker).toHaveAttribute("data-trigger", "manual_refresh");
    expect(marker).toHaveAttribute("data-review-count", "212");
    expect(marker).toHaveAttribute("data-previous-review-count", "180");
    // The timeline boundary is a separator with an accessible label.
    expect(marker).toHaveAttribute("role", "separator");
    expect(screen.getByTestId("refresh-marker-completed")).toBeInTheDocument();
    // The datetime is asserted via the machine-readable attribute, not text.
    expect(screen.getByTestId("refresh-marker-time")).toHaveAttribute(
      "dateTime",
      "2026-09-03T08:20:00Z",
    );
  });

  it.each([
    ["manual_refresh", "Refreshed manually"],
    ["duplicate_submission", "Refreshed because the URL was submitted again"],
    ["upload_replace", "Replacement file uploaded"],
  ])("maps the %s trigger to its wording", (trigger, wording) => {
    render(
      <RefreshMarker marker={makeRefreshMarker({ state: "completed", trigger })} />,
    );
    expect(screen.getByTestId("refresh-marker-trigger")).toHaveTextContent(wording);
  });

  it("falls back to neutral wording for an unknown trigger", () => {
    render(
      <RefreshMarker marker={makeRefreshMarker({ state: "completed", trigger: "mystery" })} />,
    );
    expect(screen.getByTestId("refresh-marker")).toHaveAttribute("data-trigger", "mystery");
    expect(screen.getByTestId("refresh-marker-trigger")).toHaveTextContent("Refreshed");
  });
});

describe("RefreshMarker — pending (Req 9.2)", () => {
  it("shows a spinner with an accessible label and the pending state", () => {
    render(
      <RefreshMarker
        marker={makeRefreshMarker({
          version: 2,
          state: "pending",
          completed_at: null,
          review_count: null,
          previous_review_count: null,
        })}
      />,
    );

    const marker = screen.getByTestId("refresh-marker");
    expect(marker).toHaveAttribute("data-state", "pending");
    const pending = screen.getByTestId("refresh-marker-pending");
    expect(pending).toHaveAttribute("role", "status");
    expect(screen.getByTestId("refresh-marker-spinner")).toHaveAttribute(
      "aria-label",
      "Refreshing data",
    );
    // The completed/failed detail is not shown while pending.
    expect(screen.queryByTestId("refresh-marker-completed")).not.toBeInTheDocument();
    expect(screen.queryByTestId("refresh-marker-failed")).not.toBeInTheDocument();
  });
});

describe("RefreshMarker — failed (Req 9.3)", () => {
  it("shows the failed state and references the prior version", () => {
    render(
      <RefreshMarker marker={makeRefreshMarker({ version: 3, state: "failed" })} />,
    );

    const marker = screen.getByTestId("refresh-marker");
    expect(marker).toHaveAttribute("data-state", "failed");
    expect(marker).toHaveAttribute("data-version", "3");
    const failed = screen.getByTestId("refresh-marker-failed");
    // v{n-1} — the current data version did not change.
    expect(failed).toHaveTextContent("v2");
    expect(screen.queryByTestId("refresh-marker-completed")).not.toBeInTheDocument();
  });
});
