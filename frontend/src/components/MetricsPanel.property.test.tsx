/**
 * Property-based test for the metrics panel's missing-value handling
 * (ingestion-summary task 9).
 *
 * Property 3: Missing is never zero.
 *   For any metrics object with missing fields, the metrics panel SHALL show
 *   "Not available" for each missing field and SHALL never show 0 for it.
 *   Validates: Requirement 2.5
 *
 * This renders the REAL {@link MetricsPanel} component over fast-check-generated
 * metrics objects (per the task: "Prefer testing the component's rendered
 * output for missing fields"). Each of the four tiles — review count, average
 * rating, sentiment, date range — is generated as one of:
 *   - present and valid (a real value, including a real `0`),
 *   - explicitly `null`,
 *   - absent (key deleted),
 *   - malformed (wrong type, or a partial sentiment / incomplete range),
 * and the generator records, per tile, whether that field is "available".
 *
 * The property asserts, for every generated combination:
 *   - a *missing* field (null / absent / malformed) renders the
 *     "Not available" placeholder, its tile is `data-available="false"`, and
 *     the tile text contains no bare `0`;
 *   - a *present* field is `data-available="true"` and shows no placeholder.
 * The crux (Requirement 2.5) is that a present `0` stays a number while a
 * missing field never collapses to `0`.
 *
 * Uses fast-check (frontend property testing, per the design) with Vitest and
 * Testing Library.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import fc from "fast-check";
import { afterEach, describe, expect, it } from "vitest";

import MetricsPanel from "./MetricsPanel";

afterEach(cleanup);

/** The tiles the panel renders, each with the metrics field it reads. */
const TILES = [
  { field: "review_count", testid: "metric-review-count" },
  { field: "avg_rating", testid: "metric-avg-rating" },
  { field: "sentiment", testid: "metric-sentiment" },
  { field: "date_range", testid: "metric-date-range" },
] as const;

/** A valid value for each field (present + available). */
const presentArb: Record<(typeof TILES)[number]["field"], fc.Arbitrary<unknown>> = {
  review_count: fc.integer({ min: 0, max: 100000 }),
  avg_rating: fc.float({ min: 0, max: 5, noNaN: true }),
  sentiment: fc.record({
    positive: fc.integer({ min: 0, max: 1000 }),
    neutral: fc.integer({ min: 0, max: 1000 }),
    negative: fc.integer({ min: 0, max: 1000 }),
  }),
  date_range: fc.record({
    min: fc.constantFrom("2024-01-01", "2023-06-15"),
    max: fc.constantFrom("2024-12-31", "2025-02-02"),
  }),
};

/**
 * A malformed-but-present value for each field: the right key with the wrong
 * shape, which the panel must treat as "Not available" (so a stray count can
 * never leak through). A partial sentiment and a half-empty range are the
 * realistic malformed cases.
 */
const malformedArb: Record<(typeof TILES)[number]["field"], fc.Arbitrary<unknown>> = {
  review_count: fc.constantFrom("12", null, Number.NaN),
  avg_rating: fc.constantFrom("4.2", null, Number.NaN),
  sentiment: fc.constantFrom({ positive: 10, neutral: 5 }, { positive: 1 }, 5, "x"),
  date_range: fc.constantFrom({ min: "", max: "" }, { min: "2024-01-01" }, 7, "x"),
};

/** How each tile's field is included in the generated metrics object. */
type Presence = "present" | "null" | "absent" | "malformed";

const presenceArb = fc.constantFrom<Presence>("present", "null", "absent", "malformed");

describe("Property 3: missing is never zero", () => {
  it("renders 'Not available' (never 0) for every missing tile", () => {
    fc.assert(
      fc.property(
        fc.dictionary(
          fc.constantFrom(...TILES.map((t) => t.field)),
          presenceArb,
          { minKeys: 0 },
        ),
        // Also draw concrete present/malformed values per field so the shape
        // actually varies across runs.
        fc.record({
          review_count: fc.oneof(presentArb.review_count, malformedArb.review_count),
          avg_rating: fc.oneof(presentArb.avg_rating, malformedArb.avg_rating),
          sentiment: fc.oneof(presentArb.sentiment, malformedArb.sentiment),
          date_range: fc.oneof(presentArb.date_range, malformedArb.date_range),
        }),
        (presences, _values) => {
          // Build the metrics object and the per-tile "available" expectation.
          const metrics: Record<string, unknown> = {};
          const available: Record<string, boolean> = {};

          for (const { field } of TILES) {
            const presence: Presence = presences[field] ?? "present";
            switch (presence) {
              case "present":
                metrics[field] = fc.sample(presentArb[field], 1)[0];
                available[field] = true;
                break;
              case "null":
                metrics[field] = null;
                available[field] = false;
                break;
              case "absent":
                // leave the key out entirely
                available[field] = false;
                break;
              case "malformed": {
                const value = fc.sample(malformedArb[field], 1)[0];
                metrics[field] = value;
                // null is a malformed sample too; either way it's unavailable.
                available[field] = false;
                break;
              }
            }
          }

          render(<MetricsPanel metrics={metrics} />);

          for (const { field, testid } of TILES) {
            const tile = screen.getByTestId(testid);
            if (available[field]) {
              expect(tile).toHaveAttribute("data-available", "true");
              expect(
                within(tile).queryByTestId("metric-not-available"),
              ).not.toBeInTheDocument();
            } else {
              // Missing → "Not available", and never a bare 0 in the tile.
              expect(tile).toHaveAttribute("data-available", "false");
              expect(
                within(tile).getByTestId("metric-not-available"),
              ).toBeInTheDocument();
              expect(tile).not.toHaveTextContent(/\b0\b/);
            }
          }

          cleanup();
        },
      ),
    );
  });
});
