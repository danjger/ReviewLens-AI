/**
 * Tests for {@link ThemesList} (ingestion-summary task 3.3, Requirement 2.4).
 *
 * The list shows up to 8 recurring themes, each with a label, a mention count,
 * and a sentiment-lean indicator shown with text as well as an icon (not colour
 * alone). An empty or missing `themes` list renders a small empty state.
 *
 * Tests target `data-testid`s and the structure (item count via testid, the
 * lean text) rather than brittle count-text assertions (testing.md).
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeMetrics } from "../test/fixtures";
import ThemesList from "./ThemesList";

/** Build a `metrics.themes` entry in the review-analysis shape. */
function makeTheme(
  label: string,
  mentions: number,
  lean: "positive" | "neutral" | "negative",
): Record<string, unknown> {
  return { label, mentions, lean };
}

/** The visible text expected for each lean (so colour is never the only cue). */
const LEAN_TEXT = {
  positive: "Positive",
  neutral: "Neutral",
  negative: "Negative",
} as const;

describe("ThemesList", () => {
  it("renders one item per theme with a label, count, and text sentiment lean", () => {
    const themes = [
      makeTheme("Ease of setup", 42, "positive"),
      makeTheme("Pricing", 18, "negative"),
      makeTheme("Support response time", 7, "neutral"),
    ];
    render(<ThemesList metrics={makeMetrics({ themes })} />);

    expect(screen.getByTestId("themes-list")).toHaveAttribute("data-available", "true");

    const items = screen.getAllByTestId("theme-item");
    expect(items).toHaveLength(themes.length);

    // Each item carries its label, a mentions value, and a lean shown as text.
    items.forEach((item, index) => {
      const theme = themes[index];
      expect(within(item).getByTestId("theme-label")).toHaveTextContent(
        theme.label as string,
      );
      expect(within(item).getByTestId("theme-mentions")).toBeInTheDocument();

      const leanText = within(item).getByTestId("theme-lean-text");
      expect(leanText).toHaveTextContent(LEAN_TEXT[theme.lean as keyof typeof LEAN_TEXT]);
      // The lean is also exposed structurally for styling/assertions.
      expect(within(item).getByTestId("theme-lean")).toHaveAttribute(
        "data-lean",
        theme.lean as string,
      );
    });
  });

  it("shows the sentiment lean text for every lean value", () => {
    const themes = [
      makeTheme("A", 1, "positive"),
      makeTheme("B", 1, "neutral"),
      makeTheme("C", 1, "negative"),
    ];
    render(<ThemesList metrics={makeMetrics({ themes })} />);

    expect(screen.getByText("Positive")).toBeInTheDocument();
    expect(screen.getByText("Neutral")).toBeInTheDocument();
    expect(screen.getByText("Negative")).toBeInTheDocument();
  });

  it("renders at most 8 themes even when more are supplied", () => {
    const themes = Array.from({ length: 12 }, (_, i) =>
      makeTheme(`Theme ${i + 1}`, 10 + i, "neutral"),
    );
    render(<ThemesList metrics={makeMetrics({ themes })} />);

    expect(screen.getAllByTestId("theme-item")).toHaveLength(8);
  });

  it("tolerates a degraded entry (missing count/lean) with safe defaults", () => {
    render(
      <ThemesList
        metrics={makeMetrics({ themes: [{ label: "Only a label" }] })}
      />,
    );

    const item = screen.getByTestId("theme-item");
    expect(within(item).getByTestId("theme-label")).toHaveTextContent("Only a label");
    // Missing lean falls back to neutral, still shown as text.
    expect(within(item).getByTestId("theme-lean-text")).toHaveTextContent("Neutral");
  });

  it("skips entries without a usable label", () => {
    render(
      <ThemesList
        metrics={makeMetrics({
          themes: [makeTheme("Kept", 5, "positive"), { label: "   ", mentions: 3, lean: "neutral" }],
        })}
      />,
    );

    const items = screen.getAllByTestId("theme-item");
    expect(items).toHaveLength(1);
    expect(within(items[0]).getByTestId("theme-label")).toHaveTextContent("Kept");
  });

  describe("empty / missing", () => {
    it("renders the empty state for an empty themes list", () => {
      render(<ThemesList metrics={makeMetrics({ themes: [] })} />);

      expect(screen.getByTestId("themes-list")).toHaveAttribute("data-available", "false");
      expect(screen.getByTestId("themes-empty")).toBeInTheDocument();
      expect(screen.queryByTestId("theme-item")).not.toBeInTheDocument();
    });

    it("renders the empty state when the themes key is missing", () => {
      const metrics = makeMetrics();
      delete (metrics as Record<string, unknown>).themes;
      render(<ThemesList metrics={metrics} />);

      expect(screen.getByTestId("themes-list")).toHaveAttribute("data-available", "false");
      expect(screen.getByTestId("themes-empty")).toBeInTheDocument();
    });

    it("renders the empty state when metrics is null", () => {
      render(<ThemesList metrics={null} />);

      expect(screen.getByTestId("themes-list")).toHaveAttribute("data-available", "false");
      expect(screen.getByTestId("themes-empty")).toBeInTheDocument();
    });
  });
});
