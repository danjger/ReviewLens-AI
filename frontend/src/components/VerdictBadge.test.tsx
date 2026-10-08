/**
 * Tests for {@link VerdictBadge} (dataset-ingestion task 9.4).
 *
 * The design requires all three verdicts to read as *text, not color alone*
 * ("A verdict badge (✓ Will work / ⚠ Limited / ✕ Won't work) with text, not
 * color alone"). These cases assert each verdict carries both the stable
 * `data-verdict` attribute (for other components/tests) and a human word, so
 * the verdict is never conveyed by colour by itself.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { VerdictLabel } from "../api/ingest";
import VerdictBadge from "./VerdictBadge";

describe("VerdictBadge", () => {
  const cases: Array<{ verdict: VerdictLabel; text: string }> = [
    { verdict: "will_work", text: "Will work" },
    { verdict: "limited", text: "Limited" },
    { verdict: "wont_work", text: "Won't work" },
  ];

  it.each(cases)(
    "renders the %s verdict as text, not color alone",
    ({ verdict, text }) => {
      render(<VerdictBadge verdict={verdict} />);
      const badge = screen.getByTestId("verdict-badge");
      expect(badge).toHaveAttribute("data-verdict", verdict);
      // The verdict word is present, so the meaning does not depend on colour.
      expect(badge).toHaveTextContent(text);
    },
  );

  it("hides the decorative glyph from assistive technology", () => {
    render(<VerdictBadge verdict="will_work" />);
    const badge = screen.getByTestId("verdict-badge");
    // The ✓/⚠/✕ glyph is aria-hidden; the word carries the meaning.
    const hidden = badge.querySelector("[aria-hidden='true']");
    expect(hidden).not.toBeNull();
  });
});
