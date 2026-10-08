/**
 * Tests for {@link PredictionPanel} (ingestion-summary task 4.4,
 * Requirement 7).
 *
 * The panel shows the Viability Check prediction (verdict badge, reasons,
 * warnings) next to the actual result (reviews extracted, pages captured,
 * extraction method), and highlights a large difference between the two
 * (Requirement 7.2 / Correctness Property 4). Uploads have no prediction, so
 * the panel renders an empty state.
 *
 * Tests target `data-testid`s and the panel's `data-state` / `data-highlight` /
 * `data-reason` states rather than asserting on formatted counts (testing.md).
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeEvidence, makeViability, makeViabilityActual } from "../test/fixtures";
import PredictionPanel, {
  highlightReason,
  shouldHighlightDifference,
} from "./PredictionPanel";

describe("PredictionPanel", () => {
  // ── Requirement 7.1: prediction next to actual ───────────────────────────

  describe("prediction and actual (Requirement 7.1)", () => {
    it("renders the verdict badge, reasons, warnings, and the actual result", () => {
      render(
        <PredictionPanel
          viability={makeViability({
            verdict: "will_work",
            reasons: ["24 reviews found and verified on this page"],
            warnings: ["robots.txt disallows this path"],
          })}
        />,
      );

      const panel = screen.getByTestId("prediction-panel");
      expect(panel).toHaveAttribute("data-state", "result");

      // Predicted side.
      const predicted = screen.getByTestId("prediction-predicted");
      expect(within(predicted).getByTestId("verdict-badge")).toHaveAttribute(
        "data-verdict",
        "will_work",
      );
      expect(screen.getByTestId("prediction-reasons")).toBeInTheDocument();
      expect(screen.getAllByTestId("prediction-reason")).toHaveLength(1);
      expect(screen.getByTestId("prediction-warnings")).toBeInTheDocument();
      expect(screen.getAllByTestId("prediction-warning")).toHaveLength(1);

      // Actual side.
      expect(screen.getByTestId("prediction-actual-reviews")).toBeInTheDocument();
      expect(screen.getByTestId("prediction-actual-pages")).toBeInTheDocument();
      expect(screen.getByTestId("prediction-actual-method")).toHaveAttribute(
        "data-method",
        "selectors",
      );
    });

    it("shows a pending actual when no version has completed yet", () => {
      render(<PredictionPanel viability={makeViability({ actual: undefined })} />);

      expect(screen.getByTestId("prediction-actual-pending")).toBeInTheDocument();
      expect(screen.queryByTestId("prediction-actual-reviews")).not.toBeInTheDocument();
      // Nothing to compare against: no highlight.
      expect(screen.getByTestId("prediction-panel")).toHaveAttribute(
        "data-highlight",
        "false",
      );
    });
  });

  // ── Requirement 7.2: large-difference highlight ───────────────────────────

  describe("large-difference highlight (Requirement 7.2)", () => {
    it("does NOT highlight a will_work prediction next to a matching actual", () => {
      render(
        <PredictionPanel
          viability={makeViability({
            verdict: "will_work",
            actual: makeViabilityActual({ reviews: 212 }),
          })}
        />,
      );

      expect(screen.getByTestId("prediction-panel")).toHaveAttribute(
        "data-highlight",
        "false",
      );
      expect(screen.queryByTestId("prediction-highlight")).not.toBeInTheDocument();
    });

    it("highlights a will_work prediction that yielded fewer than 5 reviews", () => {
      render(
        <PredictionPanel
          viability={makeViability({
            verdict: "will_work",
            actual: makeViabilityActual({ reviews: 4 }),
          })}
        />,
      );

      const panel = screen.getByTestId("prediction-panel");
      expect(panel).toHaveAttribute("data-highlight", "true");
      expect(panel).toHaveAttribute("data-reason", "will_work_too_few");
      expect(screen.getByTestId("prediction-highlight")).toBeInTheDocument();
    });

    it("highlights a limited prediction that yielded over 5x the detected count", () => {
      render(
        <PredictionPanel
          viability={makeViability({
            verdict: "limited",
            evidence: makeEvidence({ reviews_verified: 10 }),
            actual: makeViabilityActual({ reviews: 51 }),
          })}
        />,
      );

      const panel = screen.getByTestId("prediction-panel");
      expect(panel).toHaveAttribute("data-highlight", "true");
      expect(panel).toHaveAttribute("data-reason", "limited_overrun");
      expect(screen.getByTestId("prediction-highlight")).toBeInTheDocument();
    });

    it("does NOT highlight a limited prediction within 5x the detected count", () => {
      render(
        <PredictionPanel
          viability={makeViability({
            verdict: "limited",
            evidence: makeEvidence({ reviews_verified: 10 }),
            actual: makeViabilityActual({ reviews: 50 }),
          })}
        />,
      );

      const panel = screen.getByTestId("prediction-panel");
      expect(panel).toHaveAttribute("data-highlight", "false");
      expect(screen.queryByTestId("prediction-highlight")).not.toBeInTheDocument();
    });

    it("does NOT highlight any other verdict (wont_work)", () => {
      render(
        <PredictionPanel
          viability={makeViability({
            verdict: "wont_work",
            evidence: makeEvidence({ reviews_verified: 0 }),
            actual: makeViabilityActual({ reviews: 0 }),
          })}
        />,
      );

      expect(screen.getByTestId("prediction-panel")).toHaveAttribute(
        "data-highlight",
        "false",
      );
    });
  });

  // ── Uploads: empty state ──────────────────────────────────────────────────

  describe("uploads (no viability)", () => {
    it("renders the empty state when there is no viability block", () => {
      render(<PredictionPanel viability={null} />);

      const panel = screen.getByTestId("prediction-panel");
      expect(panel).toHaveAttribute("data-state", "empty");
      expect(screen.getByTestId("prediction-empty")).toBeInTheDocument();
      expect(screen.queryByTestId("prediction-predicted")).not.toBeInTheDocument();
    });

    it("renders the empty state when viability is undefined", () => {
      render(<PredictionPanel viability={undefined} />);

      expect(screen.getByTestId("prediction-panel")).toHaveAttribute("data-state", "empty");
    });
  });

  // ── The pure highlight rule (Correctness Property 4) ──────────────────────

  describe("highlightReason / shouldHighlightDifference (Property 4)", () => {
    it("will_work: highlights below the 5-review threshold, not at it", () => {
      const actualFew = makeViabilityActual({ reviews: 4 });
      const actualFive = makeViabilityActual({ reviews: 5 });
      expect(highlightReason("will_work", actualFew, 24)).toBe("will_work_too_few");
      expect(highlightReason("will_work", actualFive, 24)).toBeNull();
      expect(shouldHighlightDifference("will_work", actualFew, 24)).toBe(true);
      expect(shouldHighlightDifference("will_work", actualFive, 24)).toBe(false);
    });

    it("limited: highlights strictly over 5x the detected count, not exactly 5x", () => {
      const over = makeViabilityActual({ reviews: 51 });
      const exactly = makeViabilityActual({ reviews: 50 });
      expect(highlightReason("limited", over, 10)).toBe("limited_overrun");
      expect(highlightReason("limited", exactly, 10)).toBeNull();
    });

    it("limited: a zero detected count is not highlighted (no 5x baseline)", () => {
      expect(highlightReason("limited", makeViabilityActual({ reviews: 100 }), 0)).toBeNull();
    });

    it("returns null for wont_work and for a missing actual", () => {
      expect(highlightReason("wont_work", makeViabilityActual({ reviews: 0 }), 0)).toBeNull();
      expect(highlightReason("will_work", null, 24)).toBeNull();
      expect(highlightReason("limited", undefined, 10)).toBeNull();
    });
  });
});
