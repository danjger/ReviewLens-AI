/**
 * CompletenessPanel — the "how complete was the ingestion" panel on the detail
 * page (ingestion-summary task 4.2, Requirements 4.1–4.4).
 *
 * The analyst needs to know how far to trust conclusions drawn from a dataset,
 * so this panel surfaces the completeness signals the review-analysis metrics
 * stage records (design "Components and Interfaces":
 * "`CompletenessPanel`: pages captured / MAX_PAGES, extracted versus reported
 * with a percentage bar, extraction method, skipped count, and the warnings
 * list"):
 *
 * - **Pages captured out of the configured maximum (Requirement 4.1).** WHERE
 *   the dataset came from a URL, "{pages_captured} of {MAX_PAGES} pages". The
 *   maximum is the configured `MAX_PAGES` crawl cap (default 10 — see
 *   `backend/app/core/config.py` `max_pages` / `.env.example` `MAX_PAGES`);
 *   it is a backend crawl limit, not a per-dataset metric, so it is not present
 *   in the `metrics` dict. This panel therefore uses the configured default,
 *   overridable via the {@link CompletenessPanelProps.maxPages} prop. Uploads
 *   have no pages, so this line is omitted for them.
 * - **Extracted versus reported, as a percentage bar (Requirement 4.2).**
 *   WHERE the platform reported a total review count (`metrics.reported_total`),
 *   "{review_count} of {reported_total} reviews ingested" with a bar whose fill
 *   is `review_count / reported_total`. When no total was reported the bar is
 *   omitted entirely (uploads, and pages that showed no headline count).
 * - **Extraction method and page/discard/skip counts (Requirement 4.3).** The
 *   method label (page selectors found by AI, AI reading each page, structured
 *   data, or an uploaded file), how many pages were read each way
 *   (`pages_by_selectors` / `pages_by_ai`), how many AI-located items were
 *   discarded (`locator_discarded`), and the skipped rows/reviews
 *   (`metrics.skipped`).
 * - **Warnings (Requirement 4.4).** WHEN processing produced warnings
 *   (`metrics.warnings`), they are listed (e.g. "Stopped at page 4: page failed
 *   to load").
 *
 * **Data source.** Every number is read off the active-version `metrics` dict
 * produced by `app/handlers/metrics.py::compute_metrics` (mirrored in the test
 * fixture `makeMetrics`): `review_count`, `reported_total`, `pages_captured`,
 * `skipped`, `warnings`, and the nested `extraction`
 * `{method, pages_by_selectors, pages_by_ai, locator_discarded}` object. The
 * field names mirror that producer exactly; this component does not invent its
 * own shape.
 *
 * **Missing is never zero (consistent with the other panels, Requirement 2.5
 * wording).** A missing numeric field renders "Not available" rather than `0`,
 * and a block with nothing to show (no extraction method, no counts) is
 * omitted. A present `0` is a valid value and renders as the number.
 *
 * Each fact has its own `data-testid` so tests assert structure and state
 * without depending on the formatted counts (testing.md: avoid asserting on
 * text that includes counts).
 */
import type { DatasetDetail, SourceType } from "../api/datasets";

/**
 * The configured maximum pages crawled per dataset — the backend `MAX_PAGES`
 * default (`backend/app/core/config.py` `max_pages`, `.env.example`
 * `MAX_PAGES=10`). It is a crawl limit rather than a per-dataset metric, so it
 * is not carried in the `metrics` dict; the panel uses this default and lets a
 * caller override it via {@link CompletenessPanelProps.maxPages}.
 */
export const MAX_PAGES = 10;

export interface CompletenessPanelProps {
  /** The active-version metrics dict, or null until a version lands. */
  metrics: DatasetDetail["metrics"];
  /**
   * Whether the dataset came from a URL or an upload. Drives the "pages
   * captured" line, which only applies to URL datasets (Requirement 4.1).
   * Defaults to `"url"` (the common case) when not supplied.
   */
  sourceType?: SourceType;
  /**
   * The configured maximum pages (Requirement 4.1). Defaults to the backend
   * {@link MAX_PAGES} crawl cap; a caller with a different configured cap can
   * override it.
   */
  maxPages?: number;
}

/** The label shown for any count that is not available (Requirement 2.5 wording). */
const NOT_AVAILABLE = "Not available";

/**
 * Human phrasing for each extraction method the pipeline records
 * (`metrics.extraction.method`), matching Requirement 4.3's wording: "page
 * selectors found by AI, AI reading each page, structured data, or uploaded
 * file". The keys mirror the backend method vocabulary
 * (`app/handlers/metrics.py::_extraction_metrics`): `selectors`, `ai_direct`,
 * `structured`, `upload`.
 */
const METHOD_LABELS: Record<string, string> = {
  selectors: "Page selectors found by AI",
  ai_direct: "AI reading each page",
  structured: "Structured data",
  upload: "Uploaded file",
};

/** The extraction detail shape from review-analysis (`metrics.extraction`). */
interface Extraction {
  method: string;
  methodLabel: string;
  pagesBySelectors: number | null;
  pagesByAi: number | null;
  locatorDiscarded: number | null;
}

/** Read a field off the (untyped) metrics record, or `undefined` when absent. */
function field(metrics: DatasetDetail["metrics"], key: string): unknown {
  if (metrics == null) return undefined;
  return (metrics as Record<string, unknown>)[key];
}

/** True when *value* is a finite number (so `0` counts as available, `NaN` not). */
function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** The number when present and finite, else `null` ("Not available"). */
function readNumber(value: unknown): number | null {
  return isNumber(value) ? value : null;
}

/** True when *value* is a non-empty string once trimmed. */
function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/**
 * Read the `extraction` block off the metrics dict, or `null` when it is absent
 * or carries no usable method. The method label falls back to the raw method
 * string when it is an unknown value, and to `null` (whole block omitted) when
 * there is no method at all.
 */
function readExtraction(metrics: DatasetDetail["metrics"]): Extraction | null {
  const raw = field(metrics, "extraction");
  if (raw == null || typeof raw !== "object") return null;

  const record = raw as Record<string, unknown>;
  if (!nonEmptyString(record.method)) return null;

  const method = record.method.trim();
  return {
    method,
    methodLabel: METHOD_LABELS[method] ?? method,
    pagesBySelectors: readNumber(record.pages_by_selectors),
    pagesByAi: readNumber(record.pages_by_ai),
    locatorDiscarded: readNumber(record.locator_discarded),
  };
}

/** Read the warnings list, keeping only the non-empty strings. */
function readWarnings(metrics: DatasetDetail["metrics"]): string[] {
  const raw = field(metrics, "warnings");
  if (!Array.isArray(raw)) return [];
  return raw.filter(nonEmptyString).map((w) => w.trim());
}

/** The "Not available" placeholder, shown in place of a missing count. */
function NotAvailable() {
  return (
    <span className="completeness-panel__not-available" data-testid="completeness-not-available">
      {NOT_AVAILABLE}
    </span>
  );
}

export default function CompletenessPanel({
  metrics,
  sourceType = "url",
  maxPages = MAX_PAGES,
}: CompletenessPanelProps) {
  const pagesCaptured = readNumber(field(metrics, "pages_captured"));
  const reviewCount = readNumber(field(metrics, "review_count"));
  const reportedTotal = readNumber(field(metrics, "reported_total"));
  const skipped = readNumber(field(metrics, "skipped"));
  const extraction = readExtraction(metrics);
  const warnings = readWarnings(metrics);

  // The extracted-versus-reported bar only applies where the platform reported
  // a total (Requirement 4.2). Guard the divisor so a reported total of 0 (or a
  // missing review count) cannot produce a NaN/Infinity fill.
  const showReportedBar = reviewCount != null && reportedTotal != null && reportedTotal > 0;
  const percent = showReportedBar
    ? Math.min(100, Math.round(((reviewCount as number) / (reportedTotal as number)) * 100))
    : 0;

  return (
    <section
      className="completeness-panel"
      data-testid="completeness-panel"
      aria-label="Completeness"
    >
      <h2 className="completeness-panel__heading">Completeness</h2>

      {/* Pages captured out of the configured maximum — URL datasets only
          (Requirement 4.1). Uploads have no pages, so the line is omitted. */}
      {sourceType === "url" && (
        <div className="completeness-panel__pages" data-testid="completeness-pages">
          <span className="completeness-panel__label">Pages captured</span>
          <span className="completeness-panel__value" data-testid="completeness-pages-value">
            {pagesCaptured != null ? (
              <span data-testid="completeness-pages-captured">
                {pagesCaptured.toLocaleString()} of {maxPages.toLocaleString()}
              </span>
            ) : (
              <NotAvailable />
            )}
          </span>
        </div>
      )}

      {/* Extracted versus reported, as a percentage bar (Requirement 4.2).
          Rendered only where a reported total exists. */}
      {showReportedBar && (
        <div className="completeness-panel__reported" data-testid="completeness-reported">
          <span className="completeness-panel__label" data-testid="completeness-reported-label">
            <span data-testid="completeness-extracted-count">
              {(reviewCount as number).toLocaleString()}
            </span>{" "}
            of{" "}
            <span data-testid="completeness-reported-count">
              {(reportedTotal as number).toLocaleString()}
            </span>{" "}
            reviews ingested
          </span>
          <div
            className="completeness-panel__bar"
            data-testid="completeness-reported-bar"
            role="progressbar"
            aria-valuenow={percent}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="Reviews ingested out of the platform-reported total"
          >
            <div
              className="completeness-panel__bar-fill"
              data-testid="completeness-reported-bar-fill"
              style={{ width: `${percent}%` }}
            />
          </div>
        </div>
      )}

      {/* Extraction method + per-method page counts + discarded (Requirement
          4.3). Omitted when there is no extraction method to show. */}
      {extraction != null && (
        <div className="completeness-panel__extraction" data-testid="completeness-extraction">
          <div className="completeness-panel__method-row">
            <span className="completeness-panel__label">Extraction method</span>
            <span
              className="completeness-panel__value"
              data-testid="completeness-method"
              data-method={extraction.method}
            >
              {extraction.methodLabel}
            </span>
          </div>

          <dl className="completeness-panel__counts" data-testid="completeness-counts">
            <div className="completeness-panel__count" data-testid="completeness-pages-by-selectors">
              <dt className="completeness-panel__count-label">Pages read with selectors</dt>
              <dd className="completeness-panel__count-value">
                {extraction.pagesBySelectors != null ? (
                  extraction.pagesBySelectors.toLocaleString()
                ) : (
                  <NotAvailable />
                )}
              </dd>
            </div>
            <div className="completeness-panel__count" data-testid="completeness-pages-by-ai">
              <dt className="completeness-panel__count-label">Pages read by AI</dt>
              <dd className="completeness-panel__count-value">
                {extraction.pagesByAi != null ? (
                  extraction.pagesByAi.toLocaleString()
                ) : (
                  <NotAvailable />
                )}
              </dd>
            </div>
            <div className="completeness-panel__count" data-testid="completeness-locator-discarded">
              <dt className="completeness-panel__count-label">AI-located items discarded</dt>
              <dd className="completeness-panel__count-value">
                {extraction.locatorDiscarded != null ? (
                  extraction.locatorDiscarded.toLocaleString()
                ) : (
                  <NotAvailable />
                )}
              </dd>
            </div>
          </dl>
        </div>
      )}

      {/* Skipped rows/reviews (Requirement 4.3). */}
      <div className="completeness-panel__skipped" data-testid="completeness-skipped">
        <span className="completeness-panel__label">Skipped rows or reviews</span>
        <span className="completeness-panel__value" data-testid="completeness-skipped-value">
          {skipped != null ? skipped.toLocaleString() : <NotAvailable />}
        </span>
      </div>

      {/* Warnings list (Requirement 4.4). Only rendered when there is at least
          one warning; absent otherwise. */}
      {warnings.length > 0 && (
        <div className="completeness-panel__warnings" data-testid="completeness-warnings">
          <h3 className="completeness-panel__warnings-heading">Warnings</h3>
          <ul className="completeness-panel__warnings-list">
            {warnings.map((warning, index) => (
              <li
                key={index}
                className="completeness-panel__warning"
                data-testid={`completeness-warning-${index}`}
              >
                {warning}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
