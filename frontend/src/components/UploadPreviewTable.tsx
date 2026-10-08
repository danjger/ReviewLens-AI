/**
 * UploadPreviewTable — shows what the parser detected in a staged upload
 * (dataset-ingestion task 9.3, Requirements 7.2, 7.6).
 *
 * Given the {@link UploadPreview} from `POST /uploads/{id}/preview` and the
 * analyst's current mapping, it renders:
 *
 * - A **sample table**: the file's columns as headers, a few parsed rows as
 *   cells, with the header cell for each mapped column annotated with the
 *   canonical field it is mapped to (so the analyst can see the detected
 *   mapping at a glance).
 * - An **over-limit note** when `usable_rows > will_keep`: a human sentence
 *   saying how many rows are usable and which will be kept — "the most recent"
 *   when the keep rule is `most_recent_by_date`, otherwise "the first"
 *   (Requirement 7.6). The note element also carries stable `data-*` attributes
 *   (`data-over-limit`, `data-keep-rule`, `data-usable-rows`, `data-will-keep`)
 *   so tests assert the behaviour without matching the human count text
 *   (testing.md: "never depend on text that includes counts").
 *
 * It is presentational: the mapping is owned by {@link UploadTab} via
 * {@link UploadColumnMapper}; this component only reflects it.
 */
import { useMemo } from "react";

import type {
  CanonicalField,
  ColumnMapping,
  UploadPreview,
} from "../api/uploads";

export interface UploadPreviewTableProps {
  preview: UploadPreview;
  /** The analyst's current mapping (canonical field → header name). */
  mapping: ColumnMapping;
}

/** Build "header name → canonical field" from the mapping, for annotations. */
function invertMapping(mapping: ColumnMapping): Map<string, CanonicalField> {
  const byHeader = new Map<string, CanonicalField>();
  (Object.entries(mapping) as Array<[CanonicalField, string | undefined]>).forEach(
    ([fieldName, header]) => {
      if (header) byHeader.set(header, fieldName);
    },
  );
  return byHeader;
}

export default function UploadPreviewTable({
  preview,
  mapping,
}: UploadPreviewTableProps) {
  const { columns, sample_rows, usable_rows, will_keep, keep_rule } = preview;
  const overLimit = usable_rows > will_keep;

  const headerField = useMemo(() => invertMapping(mapping), [mapping]);

  const keptPhrase =
    keep_rule === "most_recent_by_date" ? "the most recent" : "the first";

  return (
    <section className="upload-preview" data-testid="upload-preview">
      <table className="upload-preview__table" data-testid="upload-preview-table">
        <thead>
          <tr>
            {columns.map((col) => {
              const field = headerField.get(col);
              return (
                <th key={col} scope="col" data-mapped-field={field ?? ""}>
                  <span className="upload-preview__col-name">{col}</span>
                  {field && (
                    <span
                      className="upload-preview__col-field"
                      data-testid="upload-preview-col-field"
                    >
                      {field}
                    </span>
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sample_rows.map((row, rowIndex) => (
            // Sample rows have no stable id; the index is a safe key for a
            // static, non-reordered preview list.
            <tr key={rowIndex} data-testid="upload-preview-row">
              {columns.map((col) => (
                <td key={col}>{row[col] ?? ""}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>

      {overLimit && (
        <p
          className="upload-preview__over-limit"
          data-testid="upload-over-limit-note"
          data-over-limit="true"
          data-keep-rule={keep_rule}
          data-usable-rows={usable_rows}
          data-will-keep={will_keep}
          role="status"
        >
          {usable_rows} rows usable; {keptPhrase} {will_keep} will be kept.
        </p>
      )}
    </section>
  );
}
