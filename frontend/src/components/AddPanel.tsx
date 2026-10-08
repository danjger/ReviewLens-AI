/**
 * AddPanel — the "Add selected" action for the New Dataset panel's URL tab
 * (dataset-ingestion task 9.2).
 *
 * It is a self-contained subtree the dataset-library panel can host beneath the
 * URL tab's results. Given the active `checkId`, the full list of Check `items`,
 * and the set of currently-included item ids (reported by the URL tab's
 * `onSelectionChange`), it:
 *
 * 1. Shows an **Add selected** button, enabled when at least one included item
 *    has a verdict that can be added (`will_work` or `limited`).
 * 2. When any included item is `limited`, opens {@link AddConfirmDialog} first,
 *    listing the limited items and how each selected item will be handled —
 *    new or refresh (Requirement 3.9). Confirming sends `confirm_limited: true`
 *    for the limited items; cancelling adds nothing.
 *    A selection of only `will_work` items is added without the prompt.
 * 3. Submits through {@link useAddItems}, which also handles the Requirement 5.4
 *    navigation rule: a single navigable result goes to its detail page; several
 *    results stay here and render {@link AddResultsSummary}.
 *
 * The URL tab (task 9.1) already filters out `wont_work` from the default
 * selection and disables its checkbox, but this panel independently refuses to
 * send a `wont_work` item, so a stale selection can never submit one.
 */
import { useCallback, useMemo, useState } from "react";

import type { AddItemInput, CheckItem } from "../api/ingest";
import {
  navigationTarget,
  useAddItems,
  type UseAddItemsOptions,
} from "../hooks/useAddItems";
import AddConfirmDialog from "./AddConfirmDialog";
import AddResultsSummary from "./AddResultsSummary";
import RateLimitMessage from "./RateLimitMessage";

export interface AddPanelProps {
  /** The active Check id, or null when no Check is loaded. */
  checkId: string | null;
  /** Every item in the current Check (used to resolve the selection). */
  items: CheckItem[];
  /** The item ids currently included via the URL tab's checkboxes. */
  includedItemIds: string[];
  /** Called when the analyst wants to run the Check again (expired case). */
  onRecheck?: () => void;
  /** Navigation override (tests pass this; defaults to the History API). */
  navigation?: UseAddItemsOptions;
}

/** True when an item's verdict is one that can actually be added. */
function isAddable(item: CheckItem): boolean {
  const label = item.verdict?.verdict;
  return label === "will_work" || label === "limited";
}

export default function AddPanel({
  checkId,
  items,
  includedItemIds,
  onRecheck,
  navigation,
}: AddPanelProps) {
  const [confirming, setConfirming] = useState(false);
  const { add, results, isAdding, rateLimit, error } = useAddItems(
    checkId,
    navigation,
  );

  const includedSet = useMemo(
    () => new Set(includedItemIds),
    [includedItemIds],
  );

  // The items that are both included and actually addable (ignores any stale
  // wont_work / non-verdict ids that might linger in the selection).
  const selectedItems = useMemo(
    () => items.filter((item) => includedSet.has(item.item_id) && isAddable(item)),
    [items, includedSet],
  );

  const hasLimited = selectedItems.some(
    (item) => item.verdict?.verdict === "limited",
  );

  const submit = useCallback(
    (limitedIds: string[] = []) => {
      const limited = new Set(limitedIds);
      const payload: AddItemInput[] = selectedItems.map((item) =>
        limited.has(item.item_id)
          ? { item_id: item.item_id, confirm_limited: true }
          : { item_id: item.item_id },
      );
      add(payload);
    },
    [selectedItems, add],
  );

  const handleAddClick = useCallback(() => {
    if (selectedItems.length === 0) return;
    if (hasLimited) {
      setConfirming(true);
      return;
    }
    submit();
  }, [selectedItems, hasLimited, submit]);

  const handleConfirm = useCallback(
    (limitedIds: string[]) => {
      setConfirming(false);
      submit(limitedIds);
    },
    [submit],
  );

  const handleCancel = useCallback(() => setConfirming(false), []);

  return (
    <section className="add-panel" data-testid="add-panel">
      <button
        type="button"
        className="add-panel__add"
        data-testid="add-selected-button"
        disabled={selectedItems.length === 0 || isAdding}
        onClick={handleAddClick}
      >
        {isAdding ? "Adding…" : "Add selected"}
      </button>

      {rateLimit && <RateLimitMessage error={rateLimit} />}

      {error && (
        <p className="add-panel__error" data-testid="add-panel-error" role="alert">
          {error.message}
        </p>
      )}

      {confirming && (
        <AddConfirmDialog
          items={selectedItems}
          onConfirm={handleConfirm}
          onCancel={handleCancel}
        />
      )}

      {/* A single navigable result navigates away (Requirement 5.4), so the
          in-place summary is for the "stay on the Library" case: several adds,
          or a batch with no single navigable dataset. */}
      {results && navigationTarget(results) == null && (
        <AddResultsSummary
          results={results}
          items={items}
          onRecheck={onRecheck}
        />
      )}
    </section>
  );
}
