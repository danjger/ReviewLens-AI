/**
 * Property-based test for the readiness derivation (ingestion-summary task 9).
 *
 * Property 2: Readiness follows the rules.
 *   For any active-version metrics and display state, the readiness indicator
 *   SHALL match Requirement 4.5.
 *   Validates: Requirement 4.5
 *
 * Requirement 4.5, as a decision table over the active version:
 *   - no active version                      → "Not ready", no note
 *   - active, no warnings AND >= 20 reviews   → "Ready for analysis"
 *   - active, (warnings OR < 20 reviews)      → "Ready with caveats"
 * and, layered on top for an active version:
 *   - display_state ready_refreshing          → note "Refreshing — showing v{n} …"
 *   - display_state ready_refresh_failed      → note "Last refresh failed — showing v{n}"
 *   - any other display_state                 → no note
 *
 * This drives the REAL exported {@link deriveReadiness} (the pure core the
 * banner renders) over fast-check-generated inputs: an optional active version,
 * every display state, and a metrics object whose `warnings`/`review_count`
 * span the threshold boundary and the malformed/missing cases the helper has to
 * tolerate. An independent oracle re-derives the expected level/note from the
 * same table, so the test pins the rule rather than re-stating the code.
 *
 * Uses fast-check (frontend property testing, per the design) with Vitest.
 */
import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { DisplayState } from "../api/datasets";
import {
  deriveReadiness,
  MIN_REVIEWS_FOR_READY,
  type ReadinessLevel,
  type ReadinessNoteKind,
} from "./ReadinessBanner";

const DISPLAY_STATES: DisplayState[] = [
  "processing",
  "ready",
  "ready_refreshing",
  "ready_refresh_failed",
  "failed",
];

/**
 * A metrics object whose fields exercise the rule boundary and the tolerated
 * malformed shapes: `review_count` spans either side of the 20-review
 * threshold (plus `undefined` and a non-number), and `warnings` spans none,
 * some, empty-string-only (which must NOT count), a missing key, and a
 * non-array. Returns the generated record together with the count of *real*
 * warnings and the numeric review count the helper should read, so the oracle
 * can mirror the helper's own leniency without re-implementing it loosely.
 */
const metricsArb: fc.Arbitrary<{
  metrics: Record<string, unknown>;
  realWarnings: number;
  reviewCount: number | null;
}> = fc
  .record({
    reviewCount: fc.oneof(
      // around the threshold (10..30 covers < 20 and >= 20)
      fc.integer({ min: 10, max: 30 }),
      fc.constant(0),
      fc.constant(undefined),
      // a non-number the helper must treat as "unknown" (→ null)
      fc.constant("lots" as unknown),
    ),
    warnings: fc.oneof(
      fc.constant(undefined),
      fc.constant([] as unknown[]),
      fc.array(fc.string({ minLength: 1, maxLength: 8 }), { maxLength: 4 }),
      // empty / whitespace-only strings must NOT count as warnings
      fc.array(fc.constantFrom("", "   "), { maxLength: 3 }),
      // a non-array the helper must treat as zero warnings
      fc.constant("oops" as unknown),
    ),
  })
  .map(({ reviewCount, warnings }) => {
    const metrics: Record<string, unknown> = {};
    if (reviewCount !== undefined) metrics.review_count = reviewCount;
    if (warnings !== undefined) metrics.warnings = warnings;

    const realWarnings = Array.isArray(warnings)
      ? (warnings as unknown[]).filter(
          (w) => typeof w === "string" && w.trim().length > 0,
        ).length
      : 0;
    const count = typeof reviewCount === "number" && Number.isFinite(reviewCount)
      ? reviewCount
      : null;
    return { metrics, realWarnings, reviewCount: count };
  });

/** Independent oracle for the level (Requirement 4.5), given no active version. */
function expectedLevel(
  activeVersion: number | null,
  realWarnings: number,
  reviewCount: number | null,
): ReadinessLevel {
  if (activeVersion == null) return "not_ready";
  const hasEnough = reviewCount != null && reviewCount >= MIN_REVIEWS_FOR_READY;
  return realWarnings === 0 && hasEnough ? "ready" : "ready_with_caveats";
}

/** Independent oracle for the note kind, given the display state + active version. */
function expectedNote(
  activeVersion: number | null,
  displayState: DisplayState,
): ReadinessNoteKind {
  if (activeVersion == null) return null;
  if (displayState === "ready_refreshing") return "refreshing";
  if (displayState === "ready_refresh_failed") return "refresh_failed";
  return null;
}

describe("Property 2: readiness follows the rules", () => {
  it("matches the Requirement 4.5 decision table for any inputs", () => {
    fc.assert(
      fc.property(
        fc.option(fc.integer({ min: 1, max: 50 }), { nil: null }),
        fc.constantFrom(...DISPLAY_STATES),
        metricsArb,
        (activeVersion, displayState, { metrics, realWarnings, reviewCount }) => {
          const result = deriveReadiness({ activeVersion, displayState, metrics });

          // Level follows the threshold table exactly.
          const level = expectedLevel(activeVersion, realWarnings, reviewCount);
          expect(result.level).toBe(level);

          // Note follows the display-state table, and only ever when there is
          // an active version to name.
          const note = expectedNote(activeVersion, displayState);
          expect(result.note).toBe(note);

          // The note text is present iff there is a note, and names the active
          // version; "Not ready" never carries a note.
          if (note == null) {
            expect(result.noteText).toBeNull();
          } else {
            expect(result.noteText).toContain(`v${activeVersion}`);
          }
          if (activeVersion == null) {
            expect(result.level).toBe("not_ready");
            expect(result.note).toBeNull();
          }

          // The reported active version echoes the input.
          expect(result.activeVersion).toBe(activeVersion);
        },
      ),
    );
  });
});
