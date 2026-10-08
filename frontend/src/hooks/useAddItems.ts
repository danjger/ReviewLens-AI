/**
 * `useAddItems` — submit the chosen Check items and decide where to go next
 * (dataset-ingestion task 9.2).
 *
 * Responsibilities:
 * - Wrap `POST /ingest/checks/{id}/add` ({@link addCheckItems}) in a TanStack
 *   Query mutation so the Add action can show a pending state and surface a
 *   `429` rate-limit error distinctly (like {@link useCheck}).
 * - After the results come back, apply the Requirement 5.4 navigation rule:
 *   when **exactly one** item resulted in a navigable dataset (a new dataset was
 *   `created`, or an existing one was `refreshed` / `restored_and_refreshed`
 *   and we have its `dataset_id`), go to that dataset's detail page. Otherwise
 *   stay put and let the caller show the per-item summary (several adds, or a
 *   batch with no single navigable dataset).
 *
 * Navigation uses the History API directly (same approach as `useCheckParam`),
 * so this subtree can be mounted without a surrounding `<Router>`. The caller
 * can override navigation (e.g. for tests, or to integrate with a real router)
 * by passing `onNavigate`.
 */
import {
  useMutation,
  type UseMutationResult,
} from "@tanstack/react-query";
import { useCallback } from "react";

import {
  ApiError,
  addCheckItems,
  type AddItemInput,
  type AddOutcome,
  type AddResult,
} from "../api/ingest";
import { datasetDetailPath } from "../routes";

/** Outcomes that correspond to a dataset the analyst can open right away. */
const NAVIGABLE_OUTCOMES: ReadonlySet<AddOutcome> = new Set<AddOutcome>([
  "created",
  "refreshed",
  "restored_and_refreshed",
]);

/**
 * Pick the single dataset to navigate to, or `null` when navigation should not
 * happen (Requirement 5.4).
 *
 * Navigation happens only when exactly one result is navigable *and* carries a
 * `dataset_id`. Several navigable results, or none, keep the analyst on the
 * Library with the summary (the "WHEN several URLs are added" branch).
 */
export function navigationTarget(results: AddResult[]): string | null {
  const navigable = results.filter(
    (r) => NAVIGABLE_OUTCOMES.has(r.outcome) && r.dataset_id != null,
  );
  if (navigable.length !== 1) return null;
  return navigable[0].dataset_id as string;
}

/** What {@link useAddItems} returns. */
export interface UseAddItemsResult {
  /** Submit the chosen items (array of `{item_id, confirm_limited?}`). */
  add: (items: AddItemInput[]) => void;
  /** The per-item results of the last successful Add, or null before one. */
  results: AddResult[] | null;
  /** True while an Add request is in flight. */
  isAdding: boolean;
  /** The rate-limit error, if the last Add hit a 429. */
  rateLimit: ApiError | null;
  /** A non-rate-limit error from the last Add, if any. */
  error: Error | null;
  /** Clear the last results/error (e.g. when starting a new selection). */
  reset: () => void;
}

export interface UseAddItemsOptions {
  /**
   * Called to navigate to a dataset detail page when exactly one navigable
   * dataset resulted. Defaults to a History-API push to
   * {@link datasetDetailPath}. Override in tests or to use a router.
   */
  onNavigate?: (path: string) => void;
}

/** Default navigation: push the detail path onto the History API. */
function defaultNavigate(path: string): void {
  if (typeof window === "undefined") return;
  window.history.pushState(null, "", path);
}

/**
 * Drive Add for the Check identified by `checkId`.
 *
 * @param checkId - the active check id, or `null` when none is active (Add is a
 *   no-op in that case).
 * @param options - optional navigation override.
 */
export function useAddItems(
  checkId: string | null,
  options: UseAddItemsOptions = {},
): UseAddItemsResult {
  const navigate = options.onNavigate ?? defaultNavigate;

  const mutation: UseMutationResult<AddResult[], Error, AddItemInput[]> =
    useMutation<AddResult[], Error, AddItemInput[]>({
      mutationFn: (items: AddItemInput[]) =>
        addCheckItems(checkId as string, items),
      onSuccess: (results) => {
        const target = navigationTarget(results);
        if (target != null) {
          navigate(datasetDetailPath(target));
        }
      },
    });

  const add = useCallback(
    (items: AddItemInput[]) => {
      if (checkId == null || items.length === 0) return;
      mutation.mutate(items);
    },
    [checkId, mutation],
  );

  const rateLimit =
    mutation.error instanceof ApiError && mutation.error.isRateLimited
      ? mutation.error
      : null;

  return {
    add,
    results: mutation.data ?? null,
    isAdding: mutation.isPending,
    rateLimit,
    error: rateLimit ? null : (mutation.error ?? null),
    reset: mutation.reset,
  };
}
