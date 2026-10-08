/**
 * MetricsPanel — the headline-metrics stat tiles on the detail page
 * (ingestion-summary task 3.1, Requirements 2.1 and 2.5).
 *
 * Shows the active version's headline numbers at a glance (Requirement 2.1):
 *
 * - **Review count** — `metrics.review_count`.
 * - **Average rating** — `metrics.avg_rating` (review-analysis sets this to
 *   `null` when no review carries a rating).
 * - **Overall sentiment breakdown** — `metrics.sentiment`, the
 *   `{positive, neutral, negative}` counts from review-analysis.
 * - **Review date range** — `metrics.date_range`, the `{min, max}` of the
 *   review dates (review-analysis sets this to `null` when no review has a
 *   date).
 *
 * The metrics object is the active-version `metrics` dict produced by the
 * review-analysis metrics stage (`app/handlers/metrics.py::compute_metrics`),
 * reached here as {@link DatasetDetail.metrics}. The field names mirror that
 * producer exactly; this component does not invent its own shape.
 *
 * **Missing is never zero (Requirement 2.5, Correctness Property 3).** WHERE a
 * metric is not available — the whole `metrics` object is null, or an
 * individual field is missing (`undefined`) or explicitly `null` (e.g. an
 * upload with no ratings) — the tile renders "Not available" rather than `0`.
 * This matters precisely because `0` is a *valid* value for a count: a missing
 * field must never be collapsed to zero. The helpers below treat only a present
 * value of the right type as "available"; anything else is "Not available".
 *
 * Each tile has its own `data-testid` so tests assert the tile structure and
 * the "Not available" state without depending on the formatted counts or dates
 * (testing.md: never assert on text with counts or dates).
 */
import type { DatasetDetail } from "../api/datasets";

export interface MetricsPanelProps {
  /** The active-version metrics dict, or null until a version lands. */
  metrics: DatasetDetail["metrics"];
}

/** The sentiment breakdown shape from review-analysis (`metrics.sentiment`). */
interface SentimentBreakdown {
  positive: number;
  neutral: number;
  negative: number;
}

/** The review date range shape from review-analysis (`metrics.date_range`). */
interface DateRange {
  min: string;
  max: string;
}

/** The label shown for any metric that is not available (Requirement 2.5). */
const NOT_AVAILABLE = "Not available";

/** Read a field off the (untyped) metrics record, or `undefined` when absent. */
function field(
  metrics: DatasetDetail["metrics"],
  key: string,
): unknown {
  if (metrics == null) return undefined;
  return (metrics as Record<string, unknown>)[key];
}

/** True when *value* is a finite number (so `0` counts as available, `NaN` not). */
function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** The sentiment breakdown when all three counts are present numbers, else null. */
function readSentiment(value: unknown): SentimentBreakdown | null {
  if (value == null || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (
    isNumber(record.positive) &&
    isNumber(record.neutral) &&
    isNumber(record.negative)
  ) {
    return {
      positive: record.positive,
      neutral: record.neutral,
      negative: record.negative,
    };
  }
  return null;
}

/** The date range when both ends are non-empty strings, else null. */
function readDateRange(value: unknown): DateRange | null {
  if (value == null || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const min = record.min;
  const max = record.max;
  if (typeof min === "string" && min.length > 0 && typeof max === "string" && max.length > 0) {
    return { min, max };
  }
  return null;
}

/** The "Not available" placeholder, shown in place of a missing metric value. */
function NotAvailable() {
  return (
    <span className="metrics-panel__not-available" data-testid="metric-not-available">
      {NOT_AVAILABLE}
    </span>
  );
}

/**
 * One stat tile. *available* decides whether the real value (*children*) or the
 * "Not available" placeholder is shown — a missing field is never rendered as a
 * number, so it can never read `0`.
 */
function StatTile({
  testid,
  label,
  available,
  children,
}: {
  testid: string;
  label: string;
  available: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className="metrics-panel__tile"
      data-testid={testid}
      data-available={available ? "true" : "false"}
    >
      <dt className="metrics-panel__tile-label">{label}</dt>
      <dd className="metrics-panel__tile-value">
        {available ? children : <NotAvailable />}
      </dd>
    </div>
  );
}

export default function MetricsPanel({ metrics }: MetricsPanelProps) {
  const reviewCount = field(metrics, "review_count");
  const avgRating = field(metrics, "avg_rating");
  const sentiment = readSentiment(field(metrics, "sentiment"));
  const dateRange = readDateRange(field(metrics, "date_range"));

  const hasReviewCount = isNumber(reviewCount);
  const hasAvgRating = isNumber(avgRating);

  return (
    <section className="metrics-panel" data-testid="metrics-panel" aria-label="Headline metrics">
      <h2 className="metrics-panel__heading">Headline metrics</h2>
      <dl className="metrics-panel__tiles" data-testid="metrics-panel-tiles">
        <StatTile testid="metric-review-count" label="Reviews" available={hasReviewCount}>
          <span className="metrics-panel__number">
            {hasReviewCount ? (reviewCount as number).toLocaleString() : null}
          </span>
        </StatTile>

        <StatTile testid="metric-avg-rating" label="Average rating" available={hasAvgRating}>
          <span className="metrics-panel__number">
            {hasAvgRating ? (avgRating as number).toLocaleString() : null}
          </span>
        </StatTile>

        <StatTile
          testid="metric-sentiment"
          label="Overall sentiment"
          available={sentiment != null}
        >
          {sentiment != null && (
            <ul className="metrics-panel__sentiment" data-testid="metric-sentiment-breakdown">
              <li
                className="metrics-panel__sentiment-item metrics-panel__sentiment-item--positive"
                data-testid="metric-sentiment-positive"
                data-sentiment="positive"
              >
                <span className="metrics-panel__sentiment-label">Positive</span>
                <span className="metrics-panel__sentiment-count">
                  {sentiment.positive.toLocaleString()}
                </span>
              </li>
              <li
                className="metrics-panel__sentiment-item metrics-panel__sentiment-item--neutral"
                data-testid="metric-sentiment-neutral"
                data-sentiment="neutral"
              >
                <span className="metrics-panel__sentiment-label">Neutral</span>
                <span className="metrics-panel__sentiment-count">
                  {sentiment.neutral.toLocaleString()}
                </span>
              </li>
              <li
                className="metrics-panel__sentiment-item metrics-panel__sentiment-item--negative"
                data-testid="metric-sentiment-negative"
                data-sentiment="negative"
              >
                <span className="metrics-panel__sentiment-label">Negative</span>
                <span className="metrics-panel__sentiment-count">
                  {sentiment.negative.toLocaleString()}
                </span>
              </li>
            </ul>
          )}
        </StatTile>

        <StatTile testid="metric-date-range" label="Review dates" available={dateRange != null}>
          {dateRange != null && (
            <span className="metrics-panel__date-range" data-testid="metric-date-range-value">
              <time className="metrics-panel__date" dateTime={dateRange.min}>
                {dateRange.min}
              </time>
              <span className="metrics-panel__date-sep" aria-hidden="true"> – </span>
              <time className="metrics-panel__date" dateTime={dateRange.max}>
                {dateRange.max}
              </time>
            </span>
          )}
        </StatTile>
      </dl>
    </section>
  );
}
