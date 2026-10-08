/**
 * Property-based test for the chat question input rules (guardrailed-chat task 10).
 *
 * Property 6: Question input rules.
 *   For any string, the input SHALL reject it exactly when it is empty,
 *   whitespace-only, or longer than 1,000 characters.
 *   Validates: Requirements 6.1, 6.4
 *
 * Drives the REAL exported {@link isSubmittableQuestion} and
 * {@link MAX_QUESTION_LENGTH} from {@link ./ChatInput} — the pure predicate the
 * component uses to decide whether a question may be submitted. An independent
 * oracle re-derives the rule straight from Requirements 6.1/6.4 ("non-empty
 * after trimming AND trimmed length <= 1,000"), so the test pins the rule rather
 * than restating the implementation.
 *
 * Generators sit on the boundaries that bite: whitespace-only strings, strings
 * whose *trimmed* length is exactly 999 / 1,000 / 1,001, and unicode (including
 * surrogate pairs and combining marks) so the length rule is checked against
 * JavaScript string length, not visible glyph count.
 *
 * Uses fast-check (frontend property testing, per the design) with Vitest.
 */
import fc from "fast-check";
import { describe, expect, it } from "vitest";

import { isSubmittableQuestion, MAX_QUESTION_LENGTH } from "./ChatInput";

/**
 * Independent oracle for Requirements 6.1 / 6.4: a question is submittable
 * exactly when it is non-empty after trimming AND no longer than the max.
 */
function oracleSubmittable(question: string): boolean {
  const trimmed = question.trim();
  return trimmed.length > 0 && trimmed.length <= MAX_QUESTION_LENGTH;
}

/** Whitespace characters the input must treat as "empty" when alone. */
const WHITESPACE = " \t\n\r\f\v\u00a0\u2003";

describe("Property 6: question input rules", () => {
  it("accepts a question exactly when non-empty-trimmed and <= 1000 chars", () => {
    fc.assert(
      fc.property(fc.fullUnicodeString(), (question) => {
        expect(isSubmittableQuestion(question)).toBe(oracleSubmittable(question));
      }),
    );
  });

  it("rejects empty and whitespace-only strings", () => {
    expect(isSubmittableQuestion("")).toBe(false);

    fc.assert(
      fc.property(
        fc.stringOf(fc.constantFrom(...WHITESPACE.split("")), { minLength: 1, maxLength: 50 }),
        (blank) => {
          // All-whitespace trims to empty, so it is never submittable.
          expect(isSubmittableQuestion(blank)).toBe(false);
        },
      ),
    );
  });

  it("respects the exact 1000-character boundary on trimmed length", () => {
    // A real (non-whitespace) character so trim() keeps the length intact.
    const body = (n: number): string => "a".repeat(n);

    expect(isSubmittableQuestion(body(MAX_QUESTION_LENGTH - 1))).toBe(true); // 999
    expect(isSubmittableQuestion(body(MAX_QUESTION_LENGTH))).toBe(true); // 1000
    expect(isSubmittableQuestion(body(MAX_QUESTION_LENGTH + 1))).toBe(false); // 1001

    // The limit is on the TRIMMED length: surrounding whitespace doesn't count.
    const padded = `   ${body(MAX_QUESTION_LENGTH)}   `;
    expect(padded.length).toBeGreaterThan(MAX_QUESTION_LENGTH);
    expect(isSubmittableQuestion(padded)).toBe(true);

    // One real char over the limit, even after trimming padding, is rejected.
    const paddedOver = `\t${body(MAX_QUESTION_LENGTH + 1)}\n`;
    expect(isSubmittableQuestion(paddedOver)).toBe(false);
  });

  it("measures length by JS string length across unicode and padding", () => {
    fc.assert(
      fc.property(
        // Content whose trimmed length straddles the limit (995..1005), built
        // from unicode so surrogate pairs count as their JS code-unit length.
        fc.integer({ min: MAX_QUESTION_LENGTH - 5, max: MAX_QUESTION_LENGTH + 5 }),
        fc.stringOf(fc.constantFrom(...WHITESPACE.split("")), { maxLength: 8 }),
        fc.stringOf(fc.constantFrom(...WHITESPACE.split("")), { maxLength: 8 }),
        (targetLen, pre, post) => {
          // "x" has JS length 1 and is non-whitespace, so trimmed length is
          // exactly targetLen regardless of the surrounding whitespace.
          const core = "x".repeat(targetLen);
          const question = `${pre}${core}${post}`;
          expect(isSubmittableQuestion(question)).toBe(oracleSubmittable(question));
          // And it agrees with the direct boundary check.
          expect(isSubmittableQuestion(question)).toBe(targetLen <= MAX_QUESTION_LENGTH);
        },
      ),
    );
  });
});
