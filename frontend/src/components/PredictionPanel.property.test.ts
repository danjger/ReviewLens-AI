/**
 * Property-based test for the prediction-versus-actual highlight rule
 * (ingestion-summary task 9).
 *
 * Property 4: Difference highlight rule.
 *   For any verdict and actual result, the prediction panel SHALL highlight
 *   exactly the cases defined in Requirement 7.2.
 *   Validates: Requirement 7.2
 *
 * Requirement 7.2 defines exactly two "large difference" cases:
 *   - a `will_work` verdict whose actual extraction yielded fewer than 5
 *     reviews;
 *   - a `limited` verdict whose actual extraction yielded over 5× the detected
 *     count (`evidence.reviews_verified`).
 * Every other verdict (`wont_work`), either defining verdict whose actual
 * result stays within bounds, and any case with no actual result yet, must NOT
 * highlight. The `limited` ratio needs a positive detected baseline (no "5× of
 * 0" to exceed).
 *
 * This drives the REAL exported {@link highlightReason} and
 * {@link shouldHighlightDifference} (the pure core the panel renders from) over
 * fast-check-generated verdict labels, actual results (including the `null` /
 * absent "no version completed" case), and detected counts spanning zero and
 * the boundary multiples. An independent oracle re-derives the expected reason
 * from Requirement 7.2 so the test pins the rule, and the generators sit the
 * values right on the boundaries (exactly 5 reviews, exactly 5×) where the
 * strict `<` / `>` comparisons bite.
 *
 * Uses fast-check (frontend property testing, per the design) with Vitest.
 */
import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { ViabilityActual } from "../api/datasets";
import type { VerdictLabel } from "../api/ingest";
import {
  highlightReason,
  type HighlightReason,
  LIMITED_OVERRUN_MULTIPLE,
  shouldHighlightDifference,
  WILL_WORK_MIN_REVIEWS,
} from "./PredictionPanel";

const VERDICTS: VerdictLabel[] = ["will_work", "limited", "wont_work"];

/** An actual result; `reviews` straddles both thresholds, incl. exact 5 / 5×. */
const actualArb: fc.Arbitrary<ViabilityActual> = fc.record({
  reviews: fc.integer({ min: 0, max: 60 }),
  pages: fc.integer({ min: 0, max: 10 }),
  method: fc.constantFrom("selectors", "ai_direct", "structured", "upload"),
  fallbacks: fc.integer({ min: 0, max: 10 }),
});

/**
 * Independent oracle for Requirement 7.2: the exact reason (or null) a verdict
 * + actual + detected count should highlight.
 */
function expectedReason(
  verdict: VerdictLabel,
  actual: ViabilityActual | null,
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

describe("Property 4: difference highlight rule", () => {
  it("highlights exactly the Requirement 7.2 cases for any inputs", () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...VERDICTS),
        // null models "no version completed yet"; otherwise a real actual.
        fc.option(actualArb, { nil: null }),
        // detected count spans 0 (no baseline) and small positive baselines
        // where the 5× boundary falls inside the reviews range.
        fc.integer({ min: 0, max: 12 }),
        (verdict, actual, detectedCount) => {
          const reason = highlightReason(verdict, actual, detectedCount);
          const expected = expectedReason(verdict, actual, detectedCount);

          // The reason is exactly the one Requirement 7.2 defines (or null).
          expect(reason).toBe(expected);

          // The boolean wrapper agrees with the reason.
          expect(shouldHighlightDifference(verdict, actual, detectedCount)).toBe(
            reason !== null,
          );

          // Only the two defining verdicts can ever highlight; wont_work and a
          // missing actual never do.
          if (reason !== null) {
            expect(actual).not.toBeNull();
            expect(verdict === "will_work" || verdict === "limited").toBe(true);
          }
          if (verdict === "wont_work" || actual == null) {
            expect(reason).toBeNull();
          }
        },
      ),
    );
  });

  it("respects the exact threshold boundaries (strict < and >)", () => {
    // 5 reviews is NOT "fewer than 5"; exactly 5× is NOT "over 5×". Pin the
    // boundary explicitly so a `<=`/`>=` regression is caught.
    const atFive: ViabilityActual = {
      reviews: WILL_WORK_MIN_REVIEWS,
      pages: 1,
      method: "selectors",
      fallbacks: 0,
    };
    expect(highlightReason("will_work", atFive, 0)).toBeNull();
    expect(highlightReason("will_work", { ...atFive, reviews: 4 }, 0)).toBe(
      "will_work_too_few",
    );

    fc.assert(
      fc.property(fc.integer({ min: 1, max: 20 }), (detected) => {
        const exactly5x: ViabilityActual = {
          reviews: LIMITED_OVERRUN_MULTIPLE * detected,
          pages: 1,
          method: "selectors",
          fallbacks: 0,
        };
        // exactly 5× detected is within bounds (not "over 5×").
        expect(highlightReason("limited", exactly5x, detected)).toBeNull();
        // one more review tips it over.
        expect(
          highlightReason("limited", { ...exactly5x, reviews: exactly5x.reviews + 1 }, detected),
        ).toBe("limited_overrun");
      }),
    );
  });
});
