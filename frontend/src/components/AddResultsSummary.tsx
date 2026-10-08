/**
 * AddResultsSummary — the per-item summary shown after Add (design "Frontend":
 * "After Add, a results summary lists each URL's outcome").
 *
 * Each result maps the backend `outcome` vocabulary to human-readable text
 * (Requirements 5.4, 6.5, 6.6):
 *
 * - `created`                → added as a new dataset
 * - `refreshed`              → existing dataset refreshed
 * - `restored_and_refreshed` → archived dataset restored and refreshed
 * - `already_refreshing`     → already being refreshed; no second refresh
 * - `refused_wont_work`      → can't be read, so it wasn't added
 * - `needs_confirmation`     → limited; needs confirmation before adding
 * - `expired`                → the Check Session expired; run the check again
 *
 * When any item is `expired`, the summary surfaces a prompt to run the Check
 * again (Requirement 5.4, design Error Handling: "the UI asks the analyst to
 * check again"), wired through `onRecheck`.
 *
 * The input URL for each row is looked up from the Check items so the analyst
 * sees which URL each outcome belongs to.
 */
import type { AddOutcome, AddResult, CheckItem } from "../api/ingest";
import { datasetDetailPath } from "../routes";

export interface AddResultsSummaryProps {
  results: AddResult[];
  /** The Check items, used to show each result's input URL. */
  items: CheckItem[];
  /** Called when the analyst chooses to run the Check again (expired case). */
  onRecheck?: () => void;
}

/** Friendly, count-free label for one outcome (safe for E2E text matching). */
function outcomeLabel(outcome: AddOutcome): string {
  switch (outcome) {
    case "created":
      return "Added as a new dataset";
    case "refreshed":
      return "Existing dataset refreshed";
    case "restored_and_refreshed":
      return "Archived dataset restored and refreshed";
    case "already_refreshing":
      return "Already being refreshed";
    case "refused_wont_work":
      return "Can't be read right now, so it wasn't added";
    case "needs_confirmation":
      return "Needs confirmation before it can be added";
    case "expired":
      return "The check expired; run it again";
    default: {
      // Exhaustiveness guard: a new outcome must be handled above.
      const never: never = outcome;
      return never;
    }
  }
}

/** Outcomes that point at a dataset the analyst can open. */
function hasDatasetLink(result: AddResult): boolean {
  return (
    result.dataset_id != null &&
    (result.outcome === "created" ||
      result.outcome === "refreshed" ||
      result.outcome === "restored_and_refreshed" ||
      result.outcome === "already_refreshing")
  );
}

export default function AddResultsSummary({
  results,
  items,
  onRecheck,
}: AddResultsSummaryProps) {
  if (results.length === 0) return null;

  const inputById = new Map(items.map((item) => [item.item_id, item.input]));
  const anyExpired = results.some((r) => r.outcome === "expired");

  return (
    <section
      className="add-summary"
      data-testid="add-results-summary"
      aria-label="Add results"
    >
      <ul className="add-summary__list">
        {results.map((result) => (
          <li
            key={result.item_id}
            className="add-summary__row"
            data-testid="add-result-row"
            data-outcome={result.outcome}
          >
            <span className="add-summary__url" data-testid="add-result-url">
              {inputById.get(result.item_id) ?? result.item_id}
            </span>
            <span className="add-summary__outcome" data-testid="add-result-outcome">
              {outcomeLabel(result.outcome)}
            </span>
            {result.message && (
              <span className="add-summary__message">{result.message}</span>
            )}
            {hasDatasetLink(result) && (
              <a
                className="add-summary__link"
                data-testid="add-result-link"
                href={datasetDetailPath(result.dataset_id as string)}
              >
                Open dataset
              </a>
            )}
          </li>
        ))}
      </ul>

      {anyExpired && (
        <div className="add-summary__expired" data-testid="add-summary-expired">
          <p>This check has expired, so these URLs weren&apos;t added.</p>
          {onRecheck && (
            <button
              type="button"
              data-testid="add-summary-recheck"
              onClick={onRecheck}
            >
              Run the check again
            </button>
          )}
        </div>
      )}
    </section>
  );
}
