/**
 * EvidenceSummary — the one-line evidence summary on a verdict card
 * (Requirement 3.7), e.g.
 *   "24 reviews found · read with page selectors · more pages found · 1,540 reported".
 *
 * Each fact is a separate element with its own `data-testid` so tests can
 * assert a specific piece of evidence without depending on counts or dates
 * rendered as free text.
 */
import type { Evidence } from "../api/ingest";

/** Human phrasing for each extraction method the plan may choose. */
const METHOD_PHRASING: Record<string, string> = {
  selectors: "read with page selectors",
  ai_direct: "AI reads each page",
  structured: "read from structured data",
};

export interface EvidenceSummaryProps {
  evidence: Evidence;
}

export default function EvidenceSummary({ evidence }: EvidenceSummaryProps) {
  const parts: React.ReactNode[] = [];

  parts.push(
    <span key="verified" data-testid="evidence-verified">
      {evidence.reviews_verified} reviews found
    </span>,
  );

  if (evidence.method) {
    const phrase = METHOD_PHRASING[evidence.method] ?? evidence.method;
    parts.push(
      <span key="method" data-testid="evidence-method" data-method={evidence.method}>
        {phrase}
      </span>,
    );
  }

  parts.push(
    <span key="pagination" data-testid="evidence-pagination">
      {evidence.pagination ? "more pages found" : "no further pages"}
    </span>,
  );

  if (evidence.reported_total != null) {
    parts.push(
      <span key="reported" data-testid="evidence-reported">
        {evidence.reported_total.toLocaleString()} reported
      </span>,
    );
  }

  if (evidence.blocker) {
    parts.push(
      <span key="blocker" data-testid="evidence-blocker" data-blocker={evidence.blocker}>
        blocker: {evidence.blocker}
      </span>,
    );
  }

  return (
    <p className="evidence-summary" data-testid="evidence-summary">
      {parts.map((part, index) => (
        <span key={index}>
          {index > 0 && <span aria-hidden="true"> · </span>}
          {part}
        </span>
      ))}
    </p>
  );
}
