/**
 * PredictionPanel — "predicted versus actual" for a URL dataset
 * (ingestion-summary task 4.4, Requirement 7).
 *
 * The analyst wants to see how the URL check's Viability Check prediction
 * compared with what was actually extracted, so they can judge the source and
 * the team can tune the check (Requirement 7). This panel shows, side by side:
 *
 * - **The prediction (Requirement 7.1).** The Viability Check verdict for the
 *   active version — its label badge ({@link VerdictBadge}, text not colour
 *   alone), plain-language reasons, and warnings — exactly as the URL tab
 *   rendered them. These come from `status_detail.viability`, the stored
 *   {@link Verdict} written by Add / refresh (`app/ingestion/service.py`).
 * - **The actual result (Requirement 7.1).** Reviews extracted, pages captured,
 *   and the extraction method used, read off `status_detail.viability.actual`
 *   (backend `completion.ActualResult`, Requirement 6.4). The field names
 *   mirror that producer exactly (`reviews`, `pages`, `method`); this component
 *   invents no shape.
 *
 * **Large-difference highlight (Requirement 7.2, Correctness Property 4).**
 * WHEN the actual result differs a lot from the prediction, the panel
 * highlights the difference. The two defined cases are a `will_work` verdict
 * that yielded fewer than 5 reviews, or a `limited` verdict that yielded over
 * 5× the detected count. That decision is the pure, exported
 * {@link shouldHighlightDifference} function so the task-9 property test can
 * reuse it; it highlights exactly those two cases and no others.
 *
 * **Uploads (no URL / viability).** An upload dataset has no Viability Check, so
 * there is no `viability` block. The panel renders an empty state rather than
 * an empty card, consistent with "WHERE the dataset came from a URL" guarding
 * both acceptance criteria.
 *
 * Each fact carries its own `data-testid`, and the highlight is exposed as a
 * `data-highlight` state on the panel (plus `data-reason`) so tests assert on
 * structure and state rather than formatted counts (testing.md).
 */
import type { Viability, ViabilityActual } from "../api/datasets";
import type { Verdict, VerdictLabel } from "../api/ingest";
import VerdictBadge from "./VerdictBadge";

export interface PredictionPanelProps {
  /**
   * The `status_detail.viability` block for the active version, or
   * `null`/`undefined` for an upload dataset (no URL check) or before the first
   * version completes.
   */
  viability?: Viability | null;
}

/**
 * The number of reviews below which a `will_work` prediction is a large
 * difference (Requirement 7.2: "a `will_work` verdict that yielded fewer than 5
 * reviews"). The threshold is "fewer than 5", so 5 is not a large difference.
 */
export const WILL_WORK_MIN_REVIEWS = 5;

/**
 * The multiple of the detected count above which a `limited` prediction is a
 * large difference (Requirement 7.2: "a `limited` verdict that yielded over 5×
 * the detected count"). The comparison is strict ("over 5×"), so exactly 5× is
 * not a large difference.
 */
export const LIMITED_OVERRUN_MULTIPLE = 5;

/** Which rule triggered the highlight (`null` when there is no highlight). */
export type HighlightReason = "will_work_too_few" | "limited_overrun" | null;

/**
 * Decide whether the actual result differs enough from the prediction to
 * highlight it — the pure core of Requirement 7.2 / Correctness Property 4.
 *
 * Highlights **exactly** the two cases Requirement 7.2 defines and no others:
 *
 * - `"will_work_too_few"` — a `will_work` verdict whose actual extraction
 *   yielded fewer than {@link WILL_WORK_MIN_REVIEWS} reviews.
 * - `"limited_overrun"` — a `limited` verdict whose actual extraction yielded
 *   over {@link LIMITED_OVERRUN_MULTIPLE}× the count the check detected
 *   (`evidence.reviews_verified`).
 *
 * Any other verdict (`wont_work`), and either defining verdict whose actual
 * result stays within bounds, returns `null` (no highlight). A missing actual
 * result (no version has completed yet) also returns `null` — there is nothing
 * to compare against.
 *
 * The `limited` rule needs a positive detected count to form the ratio; when
 * the check detected 0 reviews there is no "5× the detected count" to exceed,
 * so it is not highlighted (any positive extraction over a zero baseline is not
 * one of the two defined cases).
 *
 * This is a pure function of its inputs so the property test (task 9) can drive
 * it across generated verdict/actual pairs.
 *
 * @param verdict - The predicted verdict label.
 * @param actual - The actual processing result, or `null`/`undefined` when none
 *   has been recorded yet.
 * @param detectedCount - The reviews the check detected on the first page
 *   (`evidence.reviews_verified`), used as the `limited` baseline.
 */
export function highlightReason(
  verdict: VerdictLabel,
  actual: ViabilityActual | null | undefined,
  detectedCount: number,
): HighlightReason {
  if (actual == null) return null;

  if (verdict === "will_work") {
    return actual.reviews < WILL_WORK_MIN_REVIEWS ? "will_work_too_few" : null;
  }

  if (verdict === "limited") {
    if (detectedCount <= 0) return null;
    return actual.reviews > LIMITED_OVERRUN_MULTIPLE * detectedCount
      ? "limited_overrun"
      : null;
  }

  return null;
}

/**
 * True when the prediction-versus-actual difference should be highlighted
 * (Requirement 7.2) — a thin boolean wrapper over {@link highlightReason}.
 */
export function shouldHighlightDifference(
  verdict: VerdictLabel,
  actual: ViabilityActual | null | undefined,
  detectedCount: number,
): boolean {
  return highlightReason(verdict, actual, detectedCount) !== null;
}

/** Plain-language explanation shown next to a highlighted difference. */
const HIGHLIGHT_MESSAGES: Record<Exclude<HighlightReason, null>, string> = {
  will_work_too_few:
    "The check predicted this would work, but fewer than 5 reviews were extracted.",
  limited_overrun:
    "The check predicted a limited result, but far more reviews were extracted than it detected.",
};

/**
 * Human phrasing for each extraction method the pipeline records
 * (`viability.actual.method`), matching the Completeness panel's vocabulary so
 * the method reads the same across the page.
 */
const METHOD_LABELS: Record<string, string> = {
  selectors: "Page selectors found by AI",
  ai_direct: "AI reading each page",
  structured: "Structured data",
  upload: "Uploaded file",
};

/** The detected count the check reported (`evidence.reviews_verified`), or 0. */
function detectedCount(verdict: Verdict): number {
  const detected = verdict.evidence?.reviews_verified;
  return typeof detected === "number" && Number.isFinite(detected) ? detected : 0;
}

export default function PredictionPanel({ viability }: PredictionPanelProps) {
  // Uploads (and datasets without a recorded prediction) have no viability
  // block; render an empty state rather than an empty card (Requirement 7 is
  // guarded by "WHERE the dataset came from a URL").
  if (viability == null || typeof viability.verdict !== "string") {
    return (
      <section
        className="prediction-panel prediction-panel--empty"
        data-testid="prediction-panel"
        data-state="empty"
        aria-label="Predicted versus actual"
      >
        <h2 className="prediction-panel__heading">Predicted versus actual</h2>
        <p className="prediction-panel__empty" data-testid="prediction-empty">
          No URL check prediction for this dataset.
        </p>
      </section>
    );
  }

  const reasons = Array.isArray(viability.reasons) ? viability.reasons : [];
  const warnings = Array.isArray(viability.warnings) ? viability.warnings : [];
  const actual = viability.actual ?? null;
  const reason = highlightReason(viability.verdict, actual, detectedCount(viability));
  const highlighted = reason !== null;
  const methodLabel =
    actual != null ? METHOD_LABELS[actual.method] ?? actual.method : null;

  return (
    <section
      className="prediction-panel"
      data-testid="prediction-panel"
      data-state="result"
      data-highlight={highlighted ? "true" : "false"}
      data-reason={reason ?? undefined}
      aria-label="Predicted versus actual"
    >
      <h2 className="prediction-panel__heading">Predicted versus actual</h2>

      {/* The large-difference callout (Requirement 7.2), when triggered. */}
      {reason !== null && (
        <p
          className="prediction-panel__highlight"
          data-testid="prediction-highlight"
          role="status"
        >
          {HIGHLIGHT_MESSAGES[reason]}
        </p>
      )}

      <div className="prediction-panel__columns">
        {/* Prediction: verdict badge, reasons, warnings (Requirement 7.1). */}
        <div className="prediction-panel__predicted" data-testid="prediction-predicted">
          <h3 className="prediction-panel__column-heading">Predicted</h3>
          <VerdictBadge verdict={viability.verdict} />

          {reasons.length > 0 && (
            <ul className="prediction-panel__reasons" data-testid="prediction-reasons">
              {reasons.map((text, index) => (
                <li key={index} data-testid="prediction-reason">
                  {text}
                </li>
              ))}
            </ul>
          )}

          {warnings.length > 0 && (
            <ul className="prediction-panel__warnings" data-testid="prediction-warnings">
              {warnings.map((text, index) => (
                <li key={index} data-testid="prediction-warning">
                  {text}
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Actual: reviews extracted, pages captured, extraction method
            (Requirement 7.1). Shown once a version has completed. */}
        <div className="prediction-panel__actual" data-testid="prediction-actual">
          <h3 className="prediction-panel__column-heading">Actual</h3>
          {actual != null ? (
            <dl className="prediction-panel__facts">
              <div className="prediction-panel__fact" data-testid="prediction-actual-reviews">
                <dt className="prediction-panel__fact-label">Reviews extracted</dt>
                <dd className="prediction-panel__fact-value">
                  {actual.reviews.toLocaleString()}
                </dd>
              </div>
              <div className="prediction-panel__fact" data-testid="prediction-actual-pages">
                <dt className="prediction-panel__fact-label">Pages captured</dt>
                <dd className="prediction-panel__fact-value">
                  {actual.pages.toLocaleString()}
                </dd>
              </div>
              <div
                className="prediction-panel__fact"
                data-testid="prediction-actual-method"
                data-method={actual.method}
              >
                <dt className="prediction-panel__fact-label">Extraction method</dt>
                <dd className="prediction-panel__fact-value">{methodLabel}</dd>
              </div>
            </dl>
          ) : (
            <p
              className="prediction-panel__actual-pending"
              data-testid="prediction-actual-pending"
            >
              Not available yet.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
