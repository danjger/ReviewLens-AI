/**
 * CheckResultsList — the list of verdict cards for a Check Session.
 *
 * It renders one {@link VerdictCard} per item and owns nothing but layout; the
 * selection map and the retry/toggle callbacks are passed down from the URL tab
 * container so Add (task 9.2) can read the selection.
 */
import type { CheckItem } from "../api/ingest";
import VerdictCard from "./VerdictCard";

export interface CheckResultsListProps {
  items: CheckItem[];
  /** Map of item_id → whether its include checkbox is checked. */
  selection: Record<string, boolean>;
  onToggleInclude: (itemId: string, included: boolean) => void;
  onRetry: (itemId: string) => void;
  retrying?: boolean;
  /** Clear a terminal non-viable result from the list (local view only). */
  onDismiss?: (itemId: string) => void;
}

export default function CheckResultsList({
  items,
  selection,
  onToggleInclude,
  onRetry,
  retrying = false,
  onDismiss,
}: CheckResultsListProps) {
  if (items.length === 0) {
    return null;
  }

  return (
    <ul className="check-results" data-testid="check-results">
      {items.map((item) => (
        <VerdictCard
          key={item.item_id}
          item={item}
          included={selection[item.item_id] ?? false}
          onToggleInclude={onToggleInclude}
          onRetry={onRetry}
          retrying={retrying}
          onDismiss={onDismiss}
        />
      ))}
    </ul>
  );
}
