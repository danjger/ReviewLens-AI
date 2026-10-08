/**
 * Tests for {@link CompletenessPanel} (ingestion-summary task 4.2,
 * Requirements 4.1–4.4).
 *
 * The panel surfaces the completeness signals from the active-version `metrics`
 * dict: pages captured out of the configured maximum (Requirement 4.1), the
 * extracted-versus-reported percentage bar (Requirement 4.2), the extraction
 * method with its per-method page/discard/skip counts (Requirement 4.3), and
 * the warnings list (Requirement 4.4).
 *
 * Tests target `data-testid`s and the "Not available"/omitted states rather
 * than asserting on the formatted counts (testing.md: avoid asserting on text
 * that includes counts). The one count-ish assertion is the explicit
 * presence/absence of "Not available", which is not itself a data count.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeMetrics } from "../test/fixtures";
import CompletenessPanel, { MAX_PAGES } from "./CompletenessPanel";

describe("CompletenessPanel", () => {
  it("renders every section for a complete URL dataset", () => {
    render(<CompletenessPanel metrics={makeMetrics()} sourceType="url" />);

    expect(screen.getByTestId("completeness-panel")).toBeInTheDocument();
    expect(screen.getByTestId("completeness-pages")).toBeInTheDocument();
    expect(screen.getByTestId("completeness-reported")).toBeInTheDocument();
    expect(screen.getByTestId("completeness-extraction")).toBeInTheDocument();
    expect(screen.getByTestId("completeness-skipped")).toBeInTheDocument();
    // The default fixture has no warnings, so that block is absent.
    expect(screen.queryByTestId("completeness-warnings")).not.toBeInTheDocument();
    expect(screen.queryByTestId("completeness-not-available")).not.toBeInTheDocument();
  });

  // ── Requirement 4.1: pages captured out of the configured maximum ─────────

  describe("pages captured (Requirement 4.1)", () => {
    it("shows pages captured out of the default MAX_PAGES for a URL dataset", () => {
      render(
        <CompletenessPanel metrics={makeMetrics({ pages_captured: 7 })} sourceType="url" />,
      );

      const captured = screen.getByTestId("completeness-pages-captured");
      expect(captured).toHaveTextContent(`7 of ${MAX_PAGES}`);
    });

    it("honours an overridden maxPages", () => {
      render(
        <CompletenessPanel
          metrics={makeMetrics({ pages_captured: 3 })}
          sourceType="url"
          maxPages={5}
        />,
      );

      expect(screen.getByTestId("completeness-pages-captured")).toHaveTextContent("3 of 5");
    });

    it("omits the pages line for an upload dataset", () => {
      render(<CompletenessPanel metrics={makeMetrics()} sourceType="upload" />);

      expect(screen.queryByTestId("completeness-pages")).not.toBeInTheDocument();
    });

    it("shows 'Not available' when pages_captured is missing on a URL dataset", () => {
      const metrics = makeMetrics();
      delete (metrics as Record<string, unknown>).pages_captured;
      render(<CompletenessPanel metrics={metrics} sourceType="url" />);

      const pages = screen.getByTestId("completeness-pages");
      expect(within(pages).getByTestId("completeness-not-available")).toBeInTheDocument();
      expect(within(pages).queryByTestId("completeness-pages-captured")).not.toBeInTheDocument();
    });
  });

  // ── Requirement 4.2: extracted-versus-reported percentage bar ─────────────

  describe("extracted versus reported (Requirement 4.2)", () => {
    it("renders the percentage bar when a reported total exists", () => {
      render(
        <CompletenessPanel
          metrics={makeMetrics({ review_count: 50, reported_total: 200 })}
          sourceType="url"
        />,
      );

      const bar = screen.getByTestId("completeness-reported-bar");
      expect(bar).toHaveAttribute("role", "progressbar");
      // 50 / 200 = 25%.
      expect(bar).toHaveAttribute("aria-valuenow", "25");
      expect(screen.getByTestId("completeness-reported-bar-fill")).toHaveStyle({
        width: "25%",
      });
    });

    it("clamps the fill at 100% when more reviews were extracted than reported", () => {
      render(
        <CompletenessPanel
          metrics={makeMetrics({ review_count: 300, reported_total: 200 })}
          sourceType="url"
        />,
      );

      const bar = screen.getByTestId("completeness-reported-bar");
      expect(bar).toHaveAttribute("aria-valuenow", "100");
      expect(screen.getByTestId("completeness-reported-bar-fill")).toHaveStyle({
        width: "100%",
      });
    });

    it("omits the bar when no reported total was recorded (null)", () => {
      render(
        <CompletenessPanel metrics={makeMetrics({ reported_total: null })} sourceType="upload" />,
      );

      expect(screen.queryByTestId("completeness-reported")).not.toBeInTheDocument();
    });

    it("omits the bar when the reported total is absent", () => {
      const metrics = makeMetrics();
      delete (metrics as Record<string, unknown>).reported_total;
      render(<CompletenessPanel metrics={metrics} sourceType="url" />);

      expect(screen.queryByTestId("completeness-reported")).not.toBeInTheDocument();
    });

    it("omits the bar when the reported total is zero (no divide-by-zero)", () => {
      render(
        <CompletenessPanel
          metrics={makeMetrics({ review_count: 0, reported_total: 0 })}
          sourceType="url"
        />,
      );

      expect(screen.queryByTestId("completeness-reported")).not.toBeInTheDocument();
    });
  });

  // ── Requirement 4.3: extraction method + counts ───────────────────────────

  describe("extraction method (Requirement 4.3)", () => {
    describe.each([
      { method: "selectors", label: "Page selectors found by AI" },
      { method: "ai_direct", label: "AI reading each page" },
      { method: "structured", label: "Structured data" },
      { method: "upload", label: "Uploaded file" },
    ])("method $method", ({ method, label }) => {
      it(`renders the "${label}" label`, () => {
        render(
          <CompletenessPanel
            metrics={makeMetrics({ extraction: { ...makeMetrics().extraction as object, method } })}
            sourceType={method === "upload" ? "upload" : "url"}
          />,
        );

        const methodEl = screen.getByTestId("completeness-method");
        expect(methodEl).toHaveAttribute("data-method", method);
        expect(methodEl).toHaveTextContent(label);
      });
    });

    it("falls back to the raw method string for an unknown method", () => {
      render(
        <CompletenessPanel
          metrics={makeMetrics({
            extraction: { ...(makeMetrics().extraction as object), method: "mystery" },
          })}
          sourceType="url"
        />,
      );

      const methodEl = screen.getByTestId("completeness-method");
      expect(methodEl).toHaveAttribute("data-method", "mystery");
      expect(methodEl).toHaveTextContent("mystery");
    });

    it("renders the three per-method counts", () => {
      render(<CompletenessPanel metrics={makeMetrics()} sourceType="url" />);

      expect(screen.getByTestId("completeness-pages-by-selectors")).toBeInTheDocument();
      expect(screen.getByTestId("completeness-pages-by-ai")).toBeInTheDocument();
      expect(screen.getByTestId("completeness-locator-discarded")).toBeInTheDocument();
    });

    it("omits the extraction block when there is no method", () => {
      const metrics = makeMetrics();
      delete (metrics as Record<string, unknown>).extraction;
      render(<CompletenessPanel metrics={metrics} sourceType="url" />);

      expect(screen.queryByTestId("completeness-extraction")).not.toBeInTheDocument();
    });

    it("shows 'Not available' for a missing count inside the extraction block", () => {
      render(
        <CompletenessPanel
          metrics={makeMetrics({
            extraction: { method: "selectors" },
          })}
          sourceType="url"
        />,
      );

      const bySelectors = screen.getByTestId("completeness-pages-by-selectors");
      expect(within(bySelectors).getByTestId("completeness-not-available")).toBeInTheDocument();
    });

    it("renders the skipped count", () => {
      render(<CompletenessPanel metrics={makeMetrics({ skipped: 4 })} sourceType="url" />);

      expect(screen.getByTestId("completeness-skipped-value")).toHaveTextContent("4");
    });

    it("shows 'Not available' when skipped is missing", () => {
      const metrics = makeMetrics();
      delete (metrics as Record<string, unknown>).skipped;
      render(<CompletenessPanel metrics={metrics} sourceType="url" />);

      const skipped = screen.getByTestId("completeness-skipped");
      expect(within(skipped).getByTestId("completeness-not-available")).toBeInTheDocument();
    });

    it("renders a real 0 skipped as the number, not 'Not available'", () => {
      render(<CompletenessPanel metrics={makeMetrics({ skipped: 0 })} sourceType="url" />);

      const skipped = screen.getByTestId("completeness-skipped");
      expect(within(skipped).queryByTestId("completeness-not-available")).not.toBeInTheDocument();
      expect(screen.getByTestId("completeness-skipped-value")).toHaveTextContent("0");
    });
  });

  // ── Requirement 4.4: warnings list ────────────────────────────────────────

  describe("warnings (Requirement 4.4)", () => {
    it("lists each warning when processing produced warnings", () => {
      render(
        <CompletenessPanel
          metrics={makeMetrics({
            warnings: ["Stopped at page 4: page failed to load", "Script-only pagination"],
          })}
          sourceType="url"
        />,
      );

      const warnings = screen.getByTestId("completeness-warnings");
      expect(warnings).toBeInTheDocument();
      expect(screen.getByTestId("completeness-warning-0")).toHaveTextContent(
        "Stopped at page 4: page failed to load",
      );
      expect(screen.getByTestId("completeness-warning-1")).toHaveTextContent(
        "Script-only pagination",
      );
    });

    it("omits the warnings block when there are no warnings", () => {
      render(<CompletenessPanel metrics={makeMetrics({ warnings: [] })} sourceType="url" />);

      expect(screen.queryByTestId("completeness-warnings")).not.toBeInTheDocument();
    });

    it("omits the warnings block when the warnings field is absent", () => {
      const metrics = makeMetrics();
      delete (metrics as Record<string, unknown>).warnings;
      render(<CompletenessPanel metrics={metrics} sourceType="url" />);

      expect(screen.queryByTestId("completeness-warnings")).not.toBeInTheDocument();
    });
  });

  // ── Missing fields overall ────────────────────────────────────────────────

  describe("missing fields", () => {
    it("still renders the panel with a null metrics object", () => {
      render(<CompletenessPanel metrics={null} sourceType="url" />);

      expect(screen.getByTestId("completeness-panel")).toBeInTheDocument();
      // No reported bar (no total) and no extraction block (no method).
      expect(screen.queryByTestId("completeness-reported")).not.toBeInTheDocument();
      expect(screen.queryByTestId("completeness-extraction")).not.toBeInTheDocument();
      // Pages and skipped fall back to "Not available".
      const pages = screen.getByTestId("completeness-pages");
      expect(within(pages).getByTestId("completeness-not-available")).toBeInTheDocument();
      const skipped = screen.getByTestId("completeness-skipped");
      expect(within(skipped).getByTestId("completeness-not-available")).toBeInTheDocument();
    });

    it("renders the panel for an empty metrics object", () => {
      render(<CompletenessPanel metrics={{}} sourceType="url" />);

      expect(screen.getByTestId("completeness-panel")).toBeInTheDocument();
      expect(screen.queryByTestId("completeness-reported")).not.toBeInTheDocument();
      expect(screen.queryByTestId("completeness-extraction")).not.toBeInTheDocument();
    });
  });
});
