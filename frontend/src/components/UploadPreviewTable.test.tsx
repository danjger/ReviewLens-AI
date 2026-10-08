/**
 * Tests for {@link UploadPreviewTable} (dataset-ingestion task 9.4,
 * Requirements 7.2, 7.6).
 *
 * The preview shows the parsed columns, a few sample rows, the detected mapping
 * annotated on the header cells, and — when the file has more usable rows than
 * the cap — an over-limit note saying how many are usable and which will be
 * kept (Requirement 7.6). Per testing.md the note's behaviour is asserted via
 * stable `data-*` attributes rather than the count text, and the kept-row rule
 * (most-recent vs first) is asserted through `data-keep-rule`.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ColumnMapping, UploadPreview } from "../api/uploads";

import UploadPreviewTable from "./UploadPreviewTable";

const BASE_PREVIEW: UploadPreview = {
  columns: ["Review", "Stars", "When"],
  suggested_mapping: { text: "Review", rating: "Stars", date: "When" },
  sample_rows: [
    { Review: "Great product", Stars: "5", When: "2026-01-02" },
    { Review: "A bit pricey", Stars: "4", When: "2026-01-03" },
  ],
  usable_rows: 1200,
  will_keep: 1000,
  keep_rule: "most_recent_by_date",
};

function renderTable(
  overrides: Partial<UploadPreview> = {},
  mapping: ColumnMapping = { text: "Review", rating: "Stars", date: "When" },
) {
  render(
    <UploadPreviewTable preview={{ ...BASE_PREVIEW, ...overrides }} mapping={mapping} />,
  );
}

describe("UploadPreviewTable", () => {
  it("renders a header per column and a row per sample", () => {
    renderTable();
    const table = screen.getByTestId("upload-preview-table");
    expect(within(table).getAllByRole("columnheader")).toHaveLength(3);
    expect(screen.getAllByTestId("upload-preview-row")).toHaveLength(2);
  });

  it("annotates each mapped header with its canonical field", () => {
    renderTable({}, { text: "Review", rating: "Stars" });
    // The "Review" and "Stars" headers carry their mapped field; "When" is unmapped.
    expect(screen.getByRole("columnheader", { name: /Review/ })).toHaveAttribute(
      "data-mapped-field",
      "text",
    );
    expect(screen.getByRole("columnheader", { name: /Stars/ })).toHaveAttribute(
      "data-mapped-field",
      "rating",
    );
    expect(screen.getByRole("columnheader", { name: "When" })).toHaveAttribute(
      "data-mapped-field",
      "",
    );
  });

  it("shows the over-limit note with behaviour on data-* attributes (Req 7.6)", () => {
    renderTable({ usable_rows: 1200, will_keep: 1000, keep_rule: "most_recent_by_date" });

    const note = screen.getByTestId("upload-over-limit-note");
    expect(note).toHaveAttribute("data-over-limit", "true");
    expect(note).toHaveAttribute("data-usable-rows", "1200");
    expect(note).toHaveAttribute("data-will-keep", "1000");
    expect(note).toHaveAttribute("data-keep-rule", "most_recent_by_date");
  });

  it("says 'most recent' are kept when a date column is mapped", () => {
    renderTable({ keep_rule: "most_recent_by_date" });
    expect(screen.getByTestId("upload-over-limit-note")).toHaveTextContent(/most recent/i);
  });

  it("says 'first' rows are kept when there is no date column", () => {
    renderTable({ keep_rule: "first_in_file" });
    const note = screen.getByTestId("upload-over-limit-note");
    expect(note).toHaveAttribute("data-keep-rule", "first_in_file");
    expect(note).toHaveTextContent(/first/i);
  });

  it("omits the over-limit note when the file fits under the cap", () => {
    renderTable({ usable_rows: 20, will_keep: 20 });
    expect(screen.queryByTestId("upload-over-limit-note")).not.toBeInTheDocument();
  });
});
