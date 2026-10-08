/**
 * RatingDistributionChart — the rating-distribution bar chart on the detail
 * page (ingestion-summary task 3.2, Requirement 2.2).
 *
 * WHERE ratings exist, the detail page shows a rating distribution bar chart
 * (Requirement 2.2). Design "Components and Interfaces":
 *
 *   > `RatingDistributionChart`: horizontal bars for 5★ down to 1★, with count
 *   > and percentage labels, readable in both themes. Built with Recharts.
 *
 * **Data source.** The counts come from `metrics.rating_distribution`, the
 * `{"1": n, …, "5": n}` object produced by the review-analysis metrics stage
 * (`app/handlers/metrics.py::compute_metrics`, mirrored in the test fixture
 * `makeMetrics`). This component does not invent its own shape; it reads those
 * five star buckets off the active-version `metrics` dict.
 *
 * **Not available (Requirement 2.5 handling).** When there is no rating
 * distribution — the whole `metrics` object is null, the key is missing, or the
 * buckets sum to zero (e.g. an upload with no ratings) — the component renders
 * a small "Not available" empty state rather than an empty chart, consistent
 * with how {@link MetricsPanel} handles a missing metric.
 *
 * **Accessibility (design "Testing Strategy / Accessibility": "the chart has a
 * text table alternative").** The bars are decorative to assistive tech
 * (`aria-hidden`); the real, programmatically-available data lives in a `<table>`
 * with a row per star rating carrying the count and percentage. The data is
 * therefore never conveyed by colour or bar length alone.
 */
import {
  Bar,
  BarChart,
  Cell,
  LabelList,
  XAxis,
  YAxis,
} from "recharts";

import type { DatasetDetail } from "../api/datasets";

export interface RatingDistributionChartProps {
  /** The active-version metrics dict, or null until a version lands. */
  metrics: DatasetDetail["metrics"];
}

/** The star buckets, highest first (design: "5★ down to 1★"). */
const STARS = [5, 4, 3, 2, 1] as const;
type Star = (typeof STARS)[number];

/** The label shown when there is no rating distribution (Requirement 2.5). */
const NOT_AVAILABLE = "Not available";

/** One row of the distribution: a star rating and its review count. */
interface Bucket {
  star: Star;
  count: number;
}

/** True when *value* is a finite, non-negative number. */
function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

/**
 * Read the `{"1": n, …, "5": n}` distribution off the metrics dict into an
 * ordered 5★→1★ list of buckets, or `null` when it is absent/empty.
 *
 * Returns null when the metrics object or the key is missing, when no bucket
 * holds a valid count, or when every count is zero — all of which mean "no
 * rating distribution to show" and should render the empty state rather than a
 * chart of zero-height bars.
 */
function readDistribution(metrics: DatasetDetail["metrics"]): Bucket[] | null {
  if (metrics == null) return null;
  const raw = (metrics as Record<string, unknown>).rating_distribution;
  if (raw == null || typeof raw !== "object") return null;

  const record = raw as Record<string, unknown>;
  const buckets: Bucket[] = [];
  let total = 0;
  let anyValid = false;
  for (const star of STARS) {
    const value = record[String(star)];
    const count = isCount(value) ? value : 0;
    if (isCount(value)) anyValid = true;
    total += count;
    buckets.push({ star, count });
  }

  if (!anyValid || total === 0) return null;
  return buckets;
}

/** A theme-readable fill for each star bucket, driven by a CSS custom property. */
function barFill(star: Star): string {
  return `var(--rating-bar-${star}, currentColor)`;
}

/** The "Not available" empty state, matching MetricsPanel's wording. */
function NotAvailable() {
  return (
    <section
      className="rating-chart rating-chart--empty"
      data-testid="rating-chart"
      data-available="false"
      aria-label="Rating distribution"
    >
      <h2 className="rating-chart__heading">Rating distribution</h2>
      <p className="rating-chart__not-available" data-testid="rating-chart-not-available">
        {NOT_AVAILABLE}
      </p>
    </section>
  );
}

export default function RatingDistributionChart({
  metrics,
}: RatingDistributionChartProps) {
  const buckets = readDistribution(metrics);
  if (buckets == null) {
    return <NotAvailable />;
  }

  const total = buckets.reduce((sum, b) => sum + b.count, 0);
  /** Whole-number percentage of the total for a bucket (total > 0 here). */
  const percent = (count: number): number => Math.round((count / total) * 100);

  // Chart rows: Recharts draws top-to-bottom in array order, so 5★→1★ already
  // reads highest-at-top for a horizontal (`layout="vertical"`) bar chart.
  const chartData = buckets.map((b) => ({
    name: `${b.star}★`,
    star: b.star,
    count: b.count,
  }));

  return (
    <section
      className="rating-chart"
      data-testid="rating-chart"
      data-available="true"
      aria-label="Rating distribution"
    >
      <h2 className="rating-chart__heading">Rating distribution</h2>

      {/*
        The visual bar chart. It is purely presentational: `aria-hidden` keeps
        it out of the accessibility tree so assistive tech reads the table
        below instead (the data is never conveyed by colour/length alone).
        A fixed width/height is used rather than a ResponsiveContainer so the
        chart renders deterministically (CSS still lets the wrapper flex).
      */}
      <div className="rating-chart__graphic" data-testid="rating-chart-graphic" aria-hidden="true">
        <BarChart
          width={320}
          height={200}
          data={chartData}
          layout="vertical"
          margin={{ top: 4, right: 48, bottom: 4, left: 8 }}
        >
          <XAxis type="number" hide />
          <YAxis type="category" dataKey="name" width={32} axisLine={false} tickLine={false} />
          <Bar dataKey="count" isAnimationActive={false} radius={[0, 4, 4, 0]}>
            {chartData.map((row) => (
              <Cell key={row.star} fill={barFill(row.star)} />
            ))}
            <LabelList
              dataKey="count"
              position="right"
              formatter={(value: number) => `${value} (${percent(value)}%)`}
            />
          </Bar>
        </BarChart>
      </div>

      {/*
        The accessible text-table alternative (design Accessibility). This is
        the programmatically-available representation of the same data: a real
        <table> with a row per star rating, each carrying the count and the
        percentage of the total.
      */}
      <table className="rating-chart__table" data-testid="rating-chart-table">
        <caption className="rating-chart__caption">
          Review counts by star rating
        </caption>
        <thead>
          <tr>
            <th scope="col">Rating</th>
            <th scope="col">Reviews</th>
            <th scope="col">Share</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((bucket) => (
            <tr
              key={bucket.star}
              data-testid={`rating-chart-row-${bucket.star}`}
              data-star={bucket.star}
            >
              <th scope="row" data-testid={`rating-chart-star-${bucket.star}`}>
                {bucket.star}★
              </th>
              <td data-testid={`rating-chart-count-${bucket.star}`}>
                {bucket.count.toLocaleString()}
              </td>
              <td data-testid={`rating-chart-percent-${bucket.star}`}>
                {percent(bucket.count)}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
