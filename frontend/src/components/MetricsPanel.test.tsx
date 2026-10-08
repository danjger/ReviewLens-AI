/**
 * Tests for {@link MetricsPanel} (ingestion-summary task 3.1,
 * Requirements 2.1 and 2.5).
 *
 * The panel shows the active version's review count, average rating, overall
 * sentiment breakdown, and review date range as stat tiles (Requirement 2.1),
 * and renders "Not available" — never `0` — for any metric that is missing
 * (Requirement 2.5, Correctness Property 3: "Missing is never zero").
 *
 * Tests target the tile `data-testid`s and the "Not available" state rather
 * than the formatted counts/dates (testing.md: never assert on text with
 * counts or dates). The one count asserted on is the explicit presence/absence
 * of "Not available", which is not itself a count or date.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeMetrics } from "../test/fixtures";
import MetricsPanel from "./MetricsPanel";

/** The four headline tiles the panel renders (Requirement 2.1). */
const TILES = [
  "metric-review-count",
  "metric-avg-rating",
  "metric-sentiment",
  "metric-date-range",
] as const;

describe("MetricsPanel", () => {
  it("renders all four headline tiles for complete metrics", () => {
    render(<MetricsPanel metrics={makeMetrics()} />);

    expect(screen.getByTestId("metrics-panel")).toBeInTheDocument();
    for (const tile of TILES) {
      const el = screen.getByTestId(tile);
      expect(el).toBeInTheDocument();
      expect(el).toHaveAttribute("data-available", "true");
    }
    // No tile falls back to "Not available" when every metric is present.
    expect(screen.queryByTestId("metric-not-available")).not.toBeInTheDocument();
  });

  it("renders the three-part sentiment breakdown when sentiment is present", () => {
    render(<MetricsPanel metrics={makeMetrics()} />);

    const sentiment = screen.getByTestId("metric-sentiment-breakdown");
    expect(within(sentiment).getByTestId("metric-sentiment-positive")).toBeInTheDocument();
    expect(within(sentiment).getByTestId("metric-sentiment-neutral")).toBeInTheDocument();
    expect(within(sentiment).getByTestId("metric-sentiment-negative")).toBeInTheDocument();
  });

  // Requirement 2.5 / Property 3: each individual missing field renders
  // "Not available" and never a 0 — exercised both for an explicit null and
  // for an entirely absent key.
  describe.each([
    { field: "review_count", tile: "metric-review-count" },
    { field: "avg_rating", tile: "metric-avg-rating" },
    { field: "sentiment", tile: "metric-sentiment" },
    { field: "date_range", tile: "metric-date-range" },
  ])("missing $field", ({ field, tile }) => {
    it("shows 'Not available' when the field is null", () => {
      render(<MetricsPanel metrics={makeMetrics({ [field]: null })} />);

      const el = screen.getByTestId(tile);
      expect(el).toHaveAttribute("data-available", "false");
      expect(within(el).getByTestId("metric-not-available")).toBeInTheDocument();
      expect(within(el).getByText("Not available")).toBeInTheDocument();
      // Must never collapse a missing value to zero.
      expect(el).not.toHaveTextContent(/\b0\b/);
    });

    it("shows 'Not available' when the field is absent", () => {
      const metrics = makeMetrics();
      delete (metrics as Record<string, unknown>)[field];
      render(<MetricsPanel metrics={metrics} />);

      const el = screen.getByTestId(tile);
      expect(el).toHaveAttribute("data-available", "false");
      expect(within(el).getByTestId("metric-not-available")).toBeInTheDocument();
      expect(el).not.toHaveTextContent(/\b0\b/);
    });
  });

  it("treats a real 0 review count as available, not 'Not available'", () => {
    // A present 0 is a valid value and must render as the number, so the tile
    // is distinguishable from a missing one (the crux of Property 3).
    render(<MetricsPanel metrics={makeMetrics({ review_count: 0 })} />);

    const el = screen.getByTestId("metric-review-count");
    expect(el).toHaveAttribute("data-available", "true");
    expect(within(el).queryByTestId("metric-not-available")).not.toBeInTheDocument();
  });

  it("shows every tile as 'Not available' when metrics is null", () => {
    render(<MetricsPanel metrics={null} />);

    for (const tile of TILES) {
      const el = screen.getByTestId(tile);
      expect(el).toHaveAttribute("data-available", "false");
      expect(within(el).getByTestId("metric-not-available")).toBeInTheDocument();
    }
    expect(screen.getAllByText("Not available")).toHaveLength(TILES.length);
  });

  it("shows every tile as 'Not available' for an empty metrics object", () => {
    render(<MetricsPanel metrics={{}} />);

    for (const tile of TILES) {
      expect(screen.getByTestId(tile)).toHaveAttribute("data-available", "false");
    }
    expect(screen.getAllByText("Not available")).toHaveLength(TILES.length);
  });

  it("treats a partial sentiment object (missing a key) as not available", () => {
    // A breakdown missing one label would otherwise render a stray count; the
    // whole tile must be "Not available" rather than imply a 0 for the gap.
    render(<MetricsPanel metrics={makeMetrics({ sentiment: { positive: 10, neutral: 5 } })} />);

    const el = screen.getByTestId("metric-sentiment");
    expect(el).toHaveAttribute("data-available", "false");
    expect(within(el).getByTestId("metric-not-available")).toBeInTheDocument();
  });
});
