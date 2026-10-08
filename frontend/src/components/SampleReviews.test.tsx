/**
 * Tests for {@link SampleReviews} (dataset-ingestion task 9.4,
 * Requirement 3.7).
 *
 * The card shows "two or three sample reviews, expandable, so the analyst can
 * see what was detected". These samples are literal page text (never
 * AI-generated), so the component renders them verbatim. The cases cover: the
 * collapsed-then-expanded toggle, two and three samples, the empty case
 * (nothing rendered), and the optional rating/date meta.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeSample } from "../test/fixtures";
import SampleReviews from "./SampleReviews";

describe("SampleReviews", () => {
  it("renders nothing when there are no samples", () => {
    render(<SampleReviews samples={[]} />);
    expect(screen.queryByTestId("sample-reviews")).not.toBeInTheDocument();
  });

  it("keeps the samples collapsed until the toggle is pressed", () => {
    render(<SampleReviews samples={[makeSample(), makeSample()]} />);

    const toggle = screen.getByTestId("toggle-samples");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("sample-list")).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("sample-list")).toBeInTheDocument();
  });

  it("expands and collapses again on repeated toggles", () => {
    render(<SampleReviews samples={[makeSample()]} />);
    const toggle = screen.getByTestId("toggle-samples");

    fireEvent.click(toggle);
    expect(screen.getByTestId("sample-list")).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.queryByTestId("sample-list")).not.toBeInTheDocument();
  });

  it.each([2, 3])("shows all %i samples when expanded", (count) => {
    const samples = Array.from({ length: count }, (_, i) =>
      makeSample({ text: `Sample review ${i}` }),
    );
    render(<SampleReviews samples={samples} />);

    fireEvent.click(screen.getByTestId("toggle-samples"));
    expect(screen.getAllByTestId("sample-review")).toHaveLength(count);
    expect(screen.getAllByTestId("sample-text")).toHaveLength(count);
  });

  it("renders a sample's text verbatim and its rating/date when present", () => {
    render(
      <SampleReviews
        samples={[makeSample({ text: "Exact words from the page.", rating: 4, date: "2026-01-02" })]}
      />,
    );
    fireEvent.click(screen.getByTestId("toggle-samples"));

    expect(screen.getByTestId("sample-text")).toHaveTextContent("Exact words from the page.");
    expect(screen.getByTestId("sample-rating")).toBeInTheDocument();
    expect(screen.getByTestId("sample-date")).toBeInTheDocument();
  });

  it("omits the meta line when a sample has no rating or date", () => {
    render(<SampleReviews samples={[makeSample({ text: "No meta.", rating: null, date: null })]} />);
    fireEvent.click(screen.getByTestId("toggle-samples"));

    expect(screen.getByTestId("sample-text")).toHaveTextContent("No meta.");
    expect(screen.queryByTestId("sample-rating")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sample-date")).not.toBeInTheDocument();
  });
});
