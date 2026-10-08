/**
 * UploadColumnMapper — lets the analyst correct the detected column mapping
 * (dataset-ingestion task 9.3, Requirement 7.2).
 *
 * Renders one `<select>` per canonical field — **text** (required), and the
 * optional **rating**, **date**, **author**, and **title** — whose options are
 * the file's own columns (plus a "— none —" choice for the optional fields).
 * Choosing a column reports the updated mapping upward via `onChange`; the
 * parent ({@link UploadTab}) owns the mapping state so the preview table and the
 * submit gating stay in sync with it.
 *
 * The control is purely about the mapping; the required-name input and the
 * submit button live in {@link UploadTab}. Each select carries a stable
 * `data-testid` (`upload-map-{field}`) so tests can change a mapping and assert
 * the change propagates to submit.
 */
import type { CanonicalField, ColumnMapping } from "../api/uploads";

/** The canonical fields, in reading order; `text` is required (Req 7.2). */
const FIELDS: ReadonlyArray<{
  readonly field: CanonicalField;
  readonly label: string;
  readonly required: boolean;
}> = [
  { field: "text", label: "Review text", required: true },
  { field: "rating", label: "Rating", required: false },
  { field: "date", label: "Date", required: false },
  { field: "author", label: "Author", required: false },
  { field: "title", label: "Title", required: false },
];

/** The sentinel option value representing "no column mapped". */
const NONE = "";

export interface UploadColumnMapperProps {
  /** The file's header names (the options the analyst can choose among). */
  columns: string[];
  /** The current mapping (canonical field → header name). */
  mapping: ColumnMapping;
  /** Called with the next mapping whenever the analyst changes a select. */
  onChange: (mapping: ColumnMapping) => void;
  /** Disable the controls while a submit is in flight. */
  disabled?: boolean;
}

export default function UploadColumnMapper({
  columns,
  mapping,
  onChange,
  disabled = false,
}: UploadColumnMapperProps) {
  function handleSelect(field: CanonicalField, value: string): void {
    const next: ColumnMapping = { ...mapping };
    if (value === NONE) {
      delete next[field];
    } else {
      next[field] = value;
    }
    onChange(next);
  }

  return (
    <fieldset
      className="upload-mapper"
      data-testid="upload-column-mapper"
      disabled={disabled}
    >
      <legend>Map the file's columns</legend>
      {FIELDS.map(({ field, label, required }) => {
        const selectId = `upload-map-${field}`;
        return (
          <div className="upload-mapper__row" key={field}>
            <label htmlFor={selectId}>
              {label}
              {required && <span aria-hidden="true"> *</span>}
            </label>
            <select
              id={selectId}
              data-testid={`upload-map-${field}`}
              value={mapping[field] ?? NONE}
              onChange={(event) => handleSelect(field, event.target.value)}
            >
              {/* The optional fields can be unmapped; text must stay mapped, so
                  its "none" option is offered only to surface an invalid state
                  the submit button already guards against. */}
              <option value={NONE}>{required ? "— choose a column —" : "— none —"}</option>
              {columns.map((col) => (
                <option key={col} value={col}>
                  {col}
                </option>
              ))}
            </select>
          </div>
        );
      })}
    </fieldset>
  );
}
