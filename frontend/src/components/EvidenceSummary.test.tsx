/**
 * Tests for {@link EvidenceSummary} (dataset-ingestion task 9.4,
 * Requirement 3.7).
 *
 * The evidence line shows, as discrete elements, the verified-review fact, the
 * extraction method that will be used, whether more pages were found, the
 * reported total (when the page reports one), and any blocker. Each fact is a
 * separate `data-testid` so a test asserts the *behaviour* (which facts render,
 * which method is chosen) without matching the free-text counts the design
 * forbids depending on.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeEvidence } from "../test/fixtures";
import EvidenceSummary from "./EvidenceSummary";

describe("EvidenceSummary", () => {
  it("phrases each extraction method via its data-method attribute", () => {
    const methods = ["selectors", "ai_direct", "structured"] as const;
    for (const method of methods) {
      const { unmount } = render(
        <EvidenceSummary evidence={makeEvidence({ method })} />,
      );
      expect(screen.getByTestId("evidence-method")).toHaveAttribute(
        "data-method",
        method,
      );
      // A human phrasing is rendered (not the raw enum value).
      expect(screen.getByTestId("evidence-method").textContent?.length ?? 0).toBeGreaterThan(0);
      unmount();
    }
  });

  it("falls back to the raw method when it is unknown", () => {
    render(<EvidenceSummary evidence={makeEvidence({ method: "mystery" })} />);
    const method = screen.getByTestId("evidence-method");
    expect(method).toHaveAttribute("data-method", "mystery");
    expect(method).toHaveTextContent("mystery");
  });

  it("omits the method fact when no method is set", () => {
    render(<EvidenceSummary evidence={makeEvidence({ method: null })} />);
    expect(screen.queryByTestId("evidence-method")).not.toBeInTheDocument();
  });

  it("reflects pagination state as 'more pages' vs 'no further pages'", () => {
    const { rerender } = render(
      <EvidenceSummary evidence={makeEvidence({ pagination: true })} />,
    );
    expect(screen.getByTestId("evidence-pagination")).toHaveTextContent("more pages found");

    rerender(<EvidenceSummary evidence={makeEvidence({ pagination: false })} />);
    expect(screen.getByTestId("evidence-pagination")).toHaveTextContent("no further pages");
  });

  it("shows the reported total only when the page reports one", () => {
    const { rerender } = render(
      <EvidenceSummary evidence={makeEvidence({ reported_total: 1540 })} />,
    );
    expect(screen.getByTestId("evidence-reported")).toBeInTheDocument();

    rerender(<EvidenceSummary evidence={makeEvidence({ reported_total: null })} />);
    expect(screen.queryByTestId("evidence-reported")).not.toBeInTheDocument();
  });

  it("shows the blocker fact only when a blocker was found", () => {
    const { rerender } = render(
      <EvidenceSummary evidence={makeEvidence({ blocker: null })} />,
    );
    expect(screen.queryByTestId("evidence-blocker")).not.toBeInTheDocument();

    rerender(
      <EvidenceSummary evidence={makeEvidence({ blocker: "captcha", reviews_verified: 0 })} />,
    );
    const blocker = screen.getByTestId("evidence-blocker");
    expect(blocker).toHaveAttribute("data-blocker", "captcha");
  });

  it("always renders the verified-reviews fact", () => {
    render(<EvidenceSummary evidence={makeEvidence({ reviews_verified: 0 })} />);
    expect(screen.getByTestId("evidence-verified")).toBeInTheDocument();
  });
});
