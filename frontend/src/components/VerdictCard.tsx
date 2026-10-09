/**
 * VerdictCard — one card per checked URL in the URL tab's results list.
 *
 * Renders, per the design's "Frontend" section:
 * - the input URL and a verdict badge (text, not color alone — {@link VerdictBadge});
 * - per-URL progress while the item is still `pending`/`checking` (Requirement 1.5);
 * - plain-language reasons, warnings (including the robots disallow, Requirement
 *   3.12), and the evidence summary (Requirement 3.7);
 * - two or three expandable sample reviews ({@link SampleReviews}, Requirement 3.7);
 * - an "Already tracked" note with a link, including the tracked-`wont_work`
 *   variant ({@link TrackedNote}, Requirement 6.3);
 * - a Retry link on items that ended in `error` or timed out (Requirement 3.10);
 * - an include checkbox that is off and disabled for `wont_work`, and on by
 *   default otherwise (Requirement 3.8).
 *
 * Selection state is lifted to the parent so Add (task 9.2) can read it; this
 * card reports changes through `onToggleInclude`.
 */
import type { CheckItem } from "../api/ingest";
import EvidenceSummary from "./EvidenceSummary";
import SampleReviews from "./SampleReviews";
import TrackedNote from "./TrackedNote";
import VerdictBadge from "./VerdictBadge";

export interface VerdictCardProps {
  item: CheckItem;
  /** Whether the include checkbox is currently checked. */
  included: boolean;
  /** Called when the analyst toggles the include checkbox. */
  onToggleInclude: (itemId: string, included: boolean) => void;
  /** Called when the analyst clicks Retry on an error/timed-out item. */
  onRetry: (itemId: string) => void;
  /** True while a retry request is in flight (disables the Retry link). */
  retrying?: boolean;
  /**
   * Clear this result card from the list (local view only — does not change
   * server state). Shown for terminal non-viable items (wont_work / invalid /
   * duplicate / error) so an analyst can tidy away results they are done with.
   */
  onDismiss?: (itemId: string) => void;
}

/** The timeout reason the backend uses for a `wont_work` timeout. */
const TIMEOUT_REASON = "Page took too long to load";

/** States where the item is still being worked on. */
function isInFlight(item: CheckItem): boolean {
  return item.state === "pending" || item.state === "checking";
}

/** True when the item is terminal and NOT a viable (addable) result, so it is
 *  safe to let the analyst clear it from the list. */
function isDismissable(item: CheckItem): boolean {
  if (item.state === "invalid" || item.state === "duplicate_in_batch") return true;
  if (item.state === "error") return true;
  if (item.state === "done" && item.verdict?.verdict === "wont_work") return true;
  return false;
}

/** True when the item ended in `error` or a `wont_work` timeout (retryable). */
function isRetryable(item: CheckItem): boolean {
  if (item.state === "error") return true;
  if (item.state === "done" && item.verdict?.verdict === "wont_work") {
    return item.verdict.reasons.includes(TIMEOUT_REASON);
  }
  return false;
}

export default function VerdictCard({
  item,
  included,
  onToggleInclude,
  onRetry,
  retrying = false,
  onDismiss,
}: VerdictCardProps) {
  const verdict = item.verdict;
  const isWontWork = verdict?.verdict === "wont_work";
  // A line parsed as invalid or an in-batch duplicate never reaches a verdict.
  const isInvalid = item.state === "invalid";
  const isDuplicate = item.state === "duplicate_in_batch";

  return (
    <li className="verdict-card" data-testid="verdict-card" data-state={item.state}>
      <div className="verdict-card__header">
        <span className="verdict-card__url" data-testid="card-url">
          {item.input}
        </span>
        {verdict && <VerdictBadge verdict={verdict.verdict} />}
      </div>

      {isInFlight(item) && (
        <p className="verdict-card__progress" data-testid="card-progress" role="status">
          {item.state === "pending" ? "Queued…" : "Checking…"}
        </p>
      )}

      {isInvalid && (
        <p className="verdict-card__invalid" data-testid="card-invalid">
          {item.message ?? "This line is not a valid http or https URL."}
        </p>
      )}

      {isDuplicate && (
        <p className="verdict-card__duplicate" data-testid="card-duplicate">
          {item.message ?? "Duplicate of another URL in this submission."}
        </p>
      )}

      {verdict && (
        <>
          {verdict.reasons.length > 0 && (
            <ul className="verdict-card__reasons" data-testid="card-reasons">
              {verdict.reasons.map((reason, index) => (
                <li key={index} data-testid="card-reason">
                  {reason}
                </li>
              ))}
            </ul>
          )}

          {verdict.warnings.length > 0 && (
            <ul className="verdict-card__warnings" data-testid="card-warnings">
              {verdict.warnings.map((warning, index) => (
                <li key={index} data-testid="card-warning">
                  {warning}
                </li>
              ))}
            </ul>
          )}

          <EvidenceSummary evidence={verdict.evidence} />
          <SampleReviews samples={verdict.evidence.samples} />
        </>
      )}

      {item.existing_dataset && verdict && (
        <TrackedNote existing={item.existing_dataset} verdict={verdict.verdict} />
      )}

      <div className="verdict-card__footer">
        <label className="verdict-card__include">
          <input
            type="checkbox"
            data-testid="include-checkbox"
            checked={included}
            disabled={isWontWork || !verdict}
            onChange={(event) => onToggleInclude(item.item_id, event.target.checked)}
          />
          Include
        </label>

        {isRetryable(item) && (
          <button
            type="button"
            className="verdict-card__retry"
            data-testid="retry-link"
            disabled={retrying}
            onClick={() => onRetry(item.item_id)}
          >
            Retry
          </button>
        )}

        {onDismiss && isDismissable(item) && (
          <button
            type="button"
            className="verdict-card__dismiss"
            data-testid="dismiss-link"
            aria-label={`Clear the result for ${item.input}`}
            onClick={() => onDismiss(item.item_id)}
          >
            Clear
          </button>
        )}
      </div>
    </li>
  );
}
