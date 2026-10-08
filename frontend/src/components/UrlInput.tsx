/**
 * UrlInput — the multi-line URL textarea and the Check URLs button.
 *
 * Behaviour (Requirement 1.1, 1.5):
 * - Accepts one URL per line, up to 10 lines. Extra lines beyond 10 are kept in
 *   the textarea but a note tells the analyst only the first 10 are checked
 *   (the backend also enforces the limit).
 * - The Check button is disabled while a Check is running, so a second
 *   submission is blocked (Requirement 1.5), and while the input is empty.
 * - On submit, the non-empty lines are passed up as a `string[]`; trimming and
 *   normalization happen on the backend.
 */
import { useMemo, type FormEvent } from "react";

/** Maximum URLs accepted per submission (Requirement 1.1). */
export const MAX_URLS = 10;

export interface UrlInputProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: (urls: string[]) => void;
  /** True while a Check is running — blocks a second submission. */
  running: boolean;
}

/** Split textarea text into non-empty, trimmed lines. */
export function parseLines(value: string): string[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

export default function UrlInput({ value, onChange, onSubmit, running }: UrlInputProps) {
  const lines = useMemo(() => parseLines(value), [value]);
  const overLimit = lines.length > MAX_URLS;
  const canSubmit = lines.length > 0 && !running;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    onSubmit(lines.slice(0, MAX_URLS));
  }

  return (
    <form className="url-input" data-testid="url-input-form" onSubmit={handleSubmit}>
      <label htmlFor="url-input-textarea">Paste up to {MAX_URLS} review page URLs, one per line</label>
      <textarea
        id="url-input-textarea"
        data-testid="url-input-textarea"
        rows={6}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={"https://example.com/product/acme-crm/reviews"}
      />
      {overLimit && (
        <p className="url-input__over-limit" data-testid="url-input-over-limit" role="alert">
          Only the first {MAX_URLS} URLs will be checked.
        </p>
      )}
      <button type="submit" data-testid="check-button" disabled={!canSubmit}>
        {running ? "Checking…" : "Check URLs"}
      </button>
    </form>
  );
}
