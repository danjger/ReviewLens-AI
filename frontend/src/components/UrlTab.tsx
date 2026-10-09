/**
 * UrlTab — the contents of the New Dataset panel's URL tab (dataset-ingestion
 * task 9.1).
 *
 * The panel *layout* is owned by dataset-library; this is a self-contained
 * subtree that it can mount. It composes:
 * - {@link UrlInput}: the multi-line input (max 10) + Check button that blocks a
 *   second submission while a Check runs (Requirements 1.1, 1.5);
 * - {@link RateLimitMessage}: the 429 notice saying when checks can resume
 *   (Requirement 1.6);
 * - {@link CheckResultsList}: the per-URL verdict cards (reasons, warnings,
 *   evidence, samples, tracked notes, retry, include checkbox — Requirements
 *   3.7, 3.8, 3.10, 3.12, 6.3);
 * - {@link useCheck}: create + poll + retry; and {@link useCheckParam}: `?check=`
 *   persistence so a reload restores the results.
 *
 * Selection defaults follow Requirement 3.8: a URL is included by default once
 * it has a verdict, except `wont_work`, which is off (and the checkbox is
 * disabled in the card). The tab mounts {@link AddPanel} (task 9.2) beneath the
 * results to turn the current selection into an Add, and still exposes the
 * selection through `onSelectionChange` for a hosting panel that wants it.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { isTerminal, type CheckItem } from "../api/ingest";
import { useCheck } from "../hooks/useCheck";
import { useCheckParam } from "../hooks/useCheckParam";
import type { UseAddItemsOptions } from "../hooks/useAddItems";
import AddPanel from "./AddPanel";
import CheckResultsList from "./CheckResultsList";
import RateLimitMessage from "./RateLimitMessage";
import UrlInput from "./UrlInput";

export interface UrlTabProps {
  /** Reports the currently-included item ids (for a hosting panel). */
  onSelectionChange?: (checkId: string | null, includedItemIds: string[]) => void;
  /** Navigation override for the Add flow (tests inject this). */
  addNavigation?: UseAddItemsOptions;
}

/** Default include state for one item (Requirement 3.8). */
function defaultIncluded(item: CheckItem): boolean {
  if (!item.verdict) return false;
  return item.verdict.verdict !== "wont_work";
}

export default function UrlTab({ onSelectionChange, addNavigation }: UrlTabProps) {
  const { checkId, setCheckId } = useCheckParam();
  const [inputValue, setInputValue] = useState("");
  const [selection, setSelection] = useState<Record<string, boolean>>({});
  // Locally-dismissed result cards (analyst "Clear" on a wont_work/invalid/
  // duplicate/error item). View-only — does not touch server state.
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(new Set());

  const { items, isRunning, create, retryItem, isRetrying, rateLimit, error } =
    useCheck(checkId, setCheckId);

  // Track which items we've already defaulted, so a later poll doesn't clobber
  // an analyst's manual toggle (Requirement 3.8: on by default, then editable).
  const defaultedRef = useRef<Set<string>>(new Set());

  // Reset the "defaulted" bookkeeping when the active check changes.
  useEffect(() => {
    defaultedRef.current = new Set();
    setSelection({});
    setDismissedIds(new Set());
  }, [checkId]);

  // Apply the default include state once each item first reaches a terminal
  // state with a verdict.
  useEffect(() => {
    setSelection((previous) => {
      let changed = false;
      const next = { ...previous };
      for (const item of items) {
        if (defaultedRef.current.has(item.item_id)) continue;
        if (!isTerminal(item)) continue;
        defaultedRef.current.add(item.item_id);
        next[item.item_id] = defaultIncluded(item);
        changed = true;
      }
      return changed ? next : previous;
    });
  }, [items]);

  const includedItemIds = Object.entries(selection)
    .filter(([, included]) => included)
    .map(([itemId]) => itemId);

  // Report the selection upward for a hosting panel that wants it.
  useEffect(() => {
    if (!onSelectionChange) return;
    onSelectionChange(checkId, includedItemIds);
    // includedItemIds is derived from (checkId, selection); depending on those
    // two avoids re-running on every render from a fresh array identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onSelectionChange, checkId, selection]);

  // Clearing the active check lets the analyst start a fresh Check after an
  // expired-session summary (Requirement 5.4: "ask the analyst to check again").
  const handleRecheck = useCallback(() => {
    setCheckId(null);
  }, [setCheckId]);

  const handleSubmit = useCallback(
    (urls: string[]) => {
      create.mutate(urls);
    },
    [create],
  );

  const handleToggleInclude = useCallback((itemId: string, included: boolean) => {
    setSelection((previous) => ({ ...previous, [itemId]: included }));
  }, []);

  const handleDismiss = useCallback((itemId: string) => {
    setDismissedIds((previous) => new Set(previous).add(itemId));
  }, []);

  // Hide dismissed cards from the results (and from Add — a cleared wont_work
  // was never includable anyway).
  const visibleItems = items.filter((it) => !dismissedIds.has(it.item_id));

  return (
    <section className="url-tab" data-testid="url-tab">
      <UrlInput
        value={inputValue}
        onChange={setInputValue}
        onSubmit={handleSubmit}
        running={isRunning}
      />

      {rateLimit && <RateLimitMessage error={rateLimit} />}

      {error && (
        <p className="url-tab__error" data-testid="url-tab-error" role="alert">
          {error.message}
        </p>
      )}

      <CheckResultsList
        items={visibleItems}
        selection={selection}
        onToggleInclude={handleToggleInclude}
        onRetry={retryItem}
        retrying={isRetrying}
        onDismiss={handleDismiss}
      />

      {visibleItems.length > 0 && (
        <AddPanel
          checkId={checkId}
          items={visibleItems}
          includedItemIds={includedItemIds}
          onRecheck={handleRecheck}
          navigation={addNavigation}
        />
      )}
    </section>
  );
}
