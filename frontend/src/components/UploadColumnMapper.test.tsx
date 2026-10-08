/**
 * Tests for {@link UploadColumnMapper} (dataset-ingestion task 9.4,
 * Requirement 7.2).
 *
 * The mapper lets the analyst correct the detected column mapping. It renders a
 * `<select>` per canonical field (text required; rating/date/author/title
 * optional), seeded from the current mapping, whose options are the file's own
 * columns plus a "none" choice for the optional fields. These cases cover: the
 * selects reflect the current mapping, choosing a column reports it upward,
 * clearing an optional field removes it from the mapping, and the whole control
 * is disabled while a submit is in flight.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ColumnMapping } from "../api/uploads";
import UploadColumnMapper from "./UploadColumnMapper";

const COLUMNS = ["Review", "Stars", "When", "Who"];

function renderMapper(mapping: ColumnMapping, disabled = false) {
  const onChange = vi.fn<(m: ColumnMapping) => void>();
  render(
    <UploadColumnMapper
      columns={COLUMNS}
      mapping={mapping}
      onChange={onChange}
      disabled={disabled}
    />,
  );
  return { onChange };
}

describe("UploadColumnMapper", () => {
  it("renders a select per canonical field seeded from the mapping", () => {
    renderMapper({ text: "Review", rating: "Stars" });

    expect(screen.getByTestId("upload-map-text")).toHaveValue("Review");
    expect(screen.getByTestId("upload-map-rating")).toHaveValue("Stars");
    // Unmapped optional fields read as "none" (empty value).
    expect(screen.getByTestId("upload-map-date")).toHaveValue("");
    expect(screen.getByTestId("upload-map-author")).toHaveValue("");
    expect(screen.getByTestId("upload-map-title")).toHaveValue("");
  });

  it("reports a changed text mapping upward", () => {
    const { onChange } = renderMapper({ text: "Review" });
    fireEvent.change(screen.getByTestId("upload-map-text"), { target: { value: "When" } });
    expect(onChange).toHaveBeenCalledWith({ text: "When" });
  });

  it("adds an optional field when the analyst maps it", () => {
    const { onChange } = renderMapper({ text: "Review" });
    fireEvent.change(screen.getByTestId("upload-map-author"), { target: { value: "Who" } });
    expect(onChange).toHaveBeenCalledWith({ text: "Review", author: "Who" });
  });

  it("removes an optional field when set back to none", () => {
    const { onChange } = renderMapper({ text: "Review", rating: "Stars" });
    fireEvent.change(screen.getByTestId("upload-map-rating"), { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith({ text: "Review" });
  });

  it("offers every file column as an option for each field", () => {
    renderMapper({ text: "Review" });
    const dateSelect = screen.getByTestId("upload-map-date");
    for (const col of COLUMNS) {
      expect(within(dateSelect).getByRole("option", { name: col })).toBeInTheDocument();
    }
  });

  it("disables the whole control while submitting", () => {
    renderMapper({ text: "Review" }, true);
    // A disabled <fieldset> disables its controls.
    expect(screen.getByTestId("upload-column-mapper")).toBeDisabled();
    expect(screen.getByTestId("upload-map-text")).toBeDisabled();
  });
});
