/**
 * Tests for {@link ReadinessBanner} and its pure {@link deriveReadiness}
 * (ingestion-summary task 4.3, Requirement 4.5, Correctness Property 2).
 *
 * The banner shows one of three readiness levels for the active version and may
 * add a refreshing / refresh-failed note:
 *
 * - "Ready for analysis" — no warnings AND at least 20 reviews.
 * - "Ready with caveats" — has warnings OR fewer than 20 reviews.
 * - "Not ready" — no active version.
 * - note while refreshing / after a refresh failed, naming the active version.
 *
 * Readiness level is asserted via the `data-testid` + `data-state` pair
 * (testing.md: data-testid selectors, not count/date text). The note's version
 * numbers are stable text — not counts or dates — so they are asserted
 * directly, as task 4.3 allows.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeDatasetDetail, makeMetrics } from "../test/fixtures";
import ReadinessBanner, {
  MIN_REVIEWS_FOR_READY,
  deriveReadiness,
} from "./ReadinessBanner";
import type { DisplayState } from "../api/datasets";

describe("deriveReadiness (Correctness Property 2)", () => {
  // ── The three threshold rules ─────────────────────────────────────────────

  it("is 'ready' with no warnings and at least 20 reviews", () => {
    const readiness = deriveReadiness({
      activeVersion: 3,
      displayState: "ready",
      metrics: makeMetrics({ warnings: [], review_count: MIN_REVIEWS_FOR_READY }),
    });

    expect(readiness.level).toBe("ready");
    expect(readiness.label).toBe("Ready for analysis");
    expect(readiness.note).toBeNull();
    expect(readiness.noteText).toBeNull();
  });

  it("is 'ready_with_caveats' when there are warnings (even with enough reviews)", () => {
    const readiness = deriveReadiness({
      activeVersion: 3,
      displayState: "ready",
      metrics: makeMetrics({ warnings: ["Stopped at page 4"], review_count: 200 }),
    });

    expect(readiness.level).toBe("ready_with_caveats");
    expect(readiness.label).toBe("Ready with caveats");
  });

  it("is 'ready_with_caveats' when there are fewer than 20 reviews (even with no warnings)", () => {
    const readiness = deriveReadiness({
      activeVersion: 3,
      displayState: "ready",
      metrics: makeMetrics({ warnings: [], review_count: MIN_REVIEWS_FOR_READY - 1 }),
    });

    expect(readiness.level).toBe("ready_with_caveats");
  });

  it("is 'ready_with_caveats' when the review count is unknown (null/missing)", () => {
    const metrics = makeMetrics({ warnings: [] });
    delete (metrics as Record<string, unknown>).review_count;

    const readiness = deriveReadiness({
      activeVersion: 3,
      displayState: "ready",
      metrics,
    });

    expect(readiness.level).toBe("ready_with_caveats");
  });

  it("is 'not_ready' with no active version, and carries no note", () => {
    const readiness = deriveReadiness({
      activeVersion: null,
      displayState: "processing",
      metrics: null,
    });

    expect(readiness.level).toBe("not_ready");
    expect(readiness.label).toBe("Not ready");
    expect(readiness.note).toBeNull();
    expect(readiness.noteText).toBeNull();
    expect(readiness.activeVersion).toBeNull();
  });

  it("treats a 0 review count as fewer than 20 (caveats), not ready", () => {
    const readiness = deriveReadiness({
      activeVersion: 1,
      displayState: "ready",
      metrics: makeMetrics({ warnings: [], review_count: 0 }),
    });

    expect(readiness.level).toBe("ready_with_caveats");
  });

  it("ignores empty-string warnings when deciding the level", () => {
    const readiness = deriveReadiness({
      activeVersion: 3,
      displayState: "ready",
      metrics: makeMetrics({ warnings: ["", "   "], review_count: 50 }),
    });

    expect(readiness.level).toBe("ready");
  });

  // ── The refresh notes, with version numbers ───────────────────────────────

  it("adds the refreshing note naming the active and next version", () => {
    const readiness = deriveReadiness({
      activeVersion: 2,
      displayState: "ready_refreshing",
      metrics: makeMetrics(),
    });

    expect(readiness.note).toBe("refreshing");
    expect(readiness.noteText).toBe("Refreshing — showing v2 until v3 is ready");
  });

  it("adds the refresh-failed note naming the active version", () => {
    const readiness = deriveReadiness({
      activeVersion: 2,
      displayState: "ready_refresh_failed",
      metrics: makeMetrics(),
    });

    expect(readiness.note).toBe("refresh_failed");
    expect(readiness.noteText).toBe("Last refresh failed — showing v2");
  });

  it("keeps the underlying level while showing a refresh note", () => {
    // Refreshing a version that itself has warnings stays "caveats" + note.
    const readiness = deriveReadiness({
      activeVersion: 5,
      displayState: "ready_refreshing",
      metrics: makeMetrics({ warnings: ["partial"], review_count: 200 }),
    });

    expect(readiness.level).toBe("ready_with_caveats");
    expect(readiness.noteText).toBe("Refreshing — showing v5 until v6 is ready");
  });
});

describe("ReadinessBanner", () => {
  it("renders the 'ready' state via data-state for a healthy dataset", () => {
    render(
      <ReadinessBanner
        dataset={makeDatasetDetail({
          active_version: 3,
          display_state: "ready",
          metrics: makeMetrics({ warnings: [], review_count: 212 }),
        })}
      />,
    );

    expect(screen.getByTestId("readiness-banner")).toHaveAttribute("data-state", "ready");
    expect(screen.getByTestId("readiness-level")).toHaveTextContent("Ready for analysis");
    expect(screen.queryByTestId("readiness-note")).not.toBeInTheDocument();
  });

  it("renders 'ready_with_caveats' when the active version has warnings", () => {
    render(
      <ReadinessBanner
        dataset={makeDatasetDetail({
          active_version: 3,
          display_state: "ready",
          metrics: makeMetrics({ warnings: ["Stopped at page 4"], review_count: 200 }),
        })}
      />,
    );

    expect(screen.getByTestId("readiness-banner")).toHaveAttribute(
      "data-state",
      "ready_with_caveats",
    );
    expect(screen.getByTestId("readiness-level")).toHaveTextContent("Ready with caveats");
  });

  it("renders 'ready_with_caveats' when the active version has fewer than 20 reviews", () => {
    render(
      <ReadinessBanner
        dataset={makeDatasetDetail({
          active_version: 1,
          display_state: "ready",
          metrics: makeMetrics({ warnings: [], review_count: 5 }),
        })}
      />,
    );

    expect(screen.getByTestId("readiness-banner")).toHaveAttribute(
      "data-state",
      "ready_with_caveats",
    );
  });

  it("renders 'not_ready' and no note when there is no active version", () => {
    render(
      <ReadinessBanner
        dataset={makeDatasetDetail({
          active_version: null,
          display_state: "processing",
          metrics: null,
        })}
      />,
    );

    expect(screen.getByTestId("readiness-banner")).toHaveAttribute("data-state", "not_ready");
    expect(screen.getByTestId("readiness-level")).toHaveTextContent("Not ready");
    expect(screen.queryByTestId("readiness-note")).not.toBeInTheDocument();
  });

  it("shows the refreshing note with the active and next version numbers", () => {
    render(
      <ReadinessBanner
        dataset={makeDatasetDetail({
          active_version: 2,
          display_state: "ready_refreshing",
          metrics: makeMetrics(),
        })}
      />,
    );

    const note = screen.getByTestId("readiness-note");
    expect(note).toHaveAttribute("data-note", "refreshing");
    expect(note).toHaveTextContent("Refreshing — showing v2 until v3 is ready");
  });

  it("shows the refresh-failed note with the active version number", () => {
    render(
      <ReadinessBanner
        dataset={makeDatasetDetail({
          active_version: 2,
          display_state: "ready_refresh_failed",
          metrics: makeMetrics(),
        })}
      />,
    );

    const note = screen.getByTestId("readiness-note");
    expect(note).toHaveAttribute("data-note", "refresh_failed");
    expect(note).toHaveTextContent("Last refresh failed — showing v2");
  });

  it.each<DisplayState>(["processing", "ready", "failed"])(
    "shows no note for the %s display state",
    (displayState) => {
      render(
        <ReadinessBanner
          dataset={makeDatasetDetail({
            active_version: 3,
            display_state: displayState,
            metrics: makeMetrics(),
          })}
        />,
      );

      expect(screen.queryByTestId("readiness-note")).not.toBeInTheDocument();
    },
  );
});
