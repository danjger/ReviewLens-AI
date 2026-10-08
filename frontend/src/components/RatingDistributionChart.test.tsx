/**
 * Tests for {@link RatingDistributionChart} (ingestion-summary task 3.2,
 * Requirement 2.2).
 *
 * WHERE ratings exist the chart shows a 5★→1★ distribution with an accessible
 * text-table alternative (design "Testing Strategy / Accessibility": "the chart
 * has a text table alternative"); WHERE there is no distribution it shows a
 * "Not available" empty state consistent with the detail page's missing-metric
 * handling (Requirement 2.5).
 *
 * The table alternative is the programmatically-available representation, so
 * the tests assert on its structure (a real `<table>`, one row per star) via
 * `data-testid` + roles rather than brittle count/percentage text matches
 * (testing.md: never depend on text that includes counts).
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeMetrics } from "../test/fixtures";
import RatingDistributionChart from "./RatingDistributionChart";

/** The five star rows the table must carry, highest first. */
const STARS = [5, 4, 3, 2, 1] as const;

describe("RatingDistributionChart", () => {
  it("renders the chart with the accessible table alternative for a full distribution", () => {
    render(<RatingDistributionChart metrics={makeMetrics()} />);

    const chart = screen.getByTestId("rating-chart");
    expect(chart).toBeInTheDocument();
    expect(chart).toHaveAttribute("data-available", "true");

    // The data is available to assistive tech via a real <table> (role=table),
    // not via the bars alone.
    const table = screen.getByTestId("rating-chart-table");
    expect(table).toBeInTheDocument();
    expect(table.tagName).toBe("TABLE");
    expect(screen.getByRole("table")).toBe(table);
  });

  it("exposes one table row per star rating, 5★ down to 1★", () => {
    render(<RatingDistributionChart metrics={makeMetrics()} />);

    const table = screen.getByTestId("rating-chart-table");

    // A row per star, each with a count and a percentage cell.
    for (const star of STARS) {
      const row = within(table).getByTestId(`rating-chart-row-${star}`);
      expect(row).toBeInTheDocument();
      expect(row).toHaveAttribute("data-star", String(star));
      expect(within(row).getByTestId(`rating-chart-star-${star}`)).toBeInTheDocument();
      expect(within(row).getByTestId(`rating-chart-count-${star}`)).toBeInTheDocument();
      expect(within(row).getByTestId(`rating-chart-percent-${star}`)).toBeInTheDocument();
    }

    // Exactly the five star rows (plus the header row) — no extra buckets.
    const bodyRows = within(table).getAllByRole("row").filter((r) =>
      r.hasAttribute("data-star"),
    );
    expect(bodyRows).toHaveLength(STARS.length);
  });

  it("orders the rows highest rating first (5★ at the top)", () => {
    render(<RatingDistributionChart metrics={makeMetrics()} />);

    const table = screen.getByTestId("rating-chart-table");
    const order = within(table)
      .getAllByRole("row")
      .filter((r) => r.hasAttribute("data-star"))
      .map((r) => r.getAttribute("data-star"));

    expect(order).toEqual(STARS.map(String));
  });

  it("keeps the visual bars out of the accessibility tree (table is the source of truth)", () => {
    render(<RatingDistributionChart metrics={makeMetrics()} />);

    const graphic = screen.getByTestId("rating-chart-graphic");
    expect(graphic).toHaveAttribute("aria-hidden", "true");
  });

  it("shows the 'Not available' empty state when there is no rating distribution", () => {
    render(<RatingDistributionChart metrics={makeMetrics({ rating_distribution: null })} />);

    const chart = screen.getByTestId("rating-chart");
    expect(chart).toHaveAttribute("data-available", "false");
    expect(screen.getByTestId("rating-chart-not-available")).toBeInTheDocument();
    expect(screen.getByText("Not available")).toBeInTheDocument();
    // No table is rendered when there is nothing to show.
    expect(screen.queryByTestId("rating-chart-table")).not.toBeInTheDocument();
  });

  it("shows the empty state when the rating_distribution key is absent", () => {
    const metrics = makeMetrics();
    delete (metrics as Record<string, unknown>).rating_distribution;
    render(<RatingDistributionChart metrics={metrics} />);

    expect(screen.getByTestId("rating-chart")).toHaveAttribute("data-available", "false");
    expect(screen.getByTestId("rating-chart-not-available")).toBeInTheDocument();
  });

  it("shows the empty state when every bucket is zero (upload with no ratings)", () => {
    render(
      <RatingDistributionChart
        metrics={makeMetrics({
          rating_distribution: { "1": 0, "2": 0, "3": 0, "4": 0, "5": 0 },
        })}
      />,
    );

    expect(screen.getByTestId("rating-chart")).toHaveAttribute("data-available", "false");
    expect(screen.queryByTestId("rating-chart-table")).not.toBeInTheDocument();
  });

  it("shows the empty state when metrics is null", () => {
    render(<RatingDistributionChart metrics={null} />);

    expect(screen.getByTestId("rating-chart")).toHaveAttribute("data-available", "false");
    expect(screen.getByTestId("rating-chart-not-available")).toBeInTheDocument();
  });
});
