/**
 * useActiveVersionRefetch — detail-page live wiring for an active-version
 * change (ingestion-summary task 6.2, Requirement 6.3).
 *
 * The real-time channel is owned by `dataset-library`:
 *   - {@link useRealtime} opens the single tab WebSocket;
 *   - `realtimeReducers.applyDatasetStatusChanged` patches `['dataset', id]`
 *     (and the `['datasets']` list) in place when a `dataset.status.changed`
 *     frame arrives — including the dataset's `active_version`.
 * That reducer deliberately does NOT touch `['reviews', id]` or
 * `['snapshot', id]`: those are the detail page's concern. Per the spec-workflow
 * cross-spec rule we must NOT change the dataset-library reducer, so this hook
 * adds the missing behaviour entirely inside the ingestion-summary layer.
 *
 * How it works (design "Live behavior"): the detail page already reads the
 * dataset through `useDataset`, keyed on the same `['dataset', id]` entry the
 * reducer patches. This hook watches the `active_version` on that query result.
 * When it transitions from one defined version to a *different* defined version
 * (the reducer having patched the new value in), it invalidates:
 *   - `['reviews', id]` — a key *prefix*, so every cached page/filter/search
 *     combination for the dataset refetches the new version's reviews
 *     (`reviewsQueryKey` puts the params in a trailing segment for exactly this);
 *   - `['snapshot', id]` — so the new version's pre-signed snapshot URL reloads.
 * The detail record itself (`['dataset', id]`) is already current — the reducer
 * patched it from the frame — so it is also invalidated here to pull the full
 * new-version record (metrics, `status_detail`, versions) the lightweight frame
 * does not carry, matching the design's "also refetches the detail".
 *
 * The comparison is against the PREVIOUS render's `active_version`, so the
 * invalidation fires on the transition (old → new) rather than on every render.
 * The first observed value (initial load, or a load that goes straight to a
 * version) is only recorded, never treated as a change — there is no prior
 * version to move away from, and the queries fetch their initial data on mount
 * on their own.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { datasetQueryKey } from "./realtimeReducers";
import { snapshotQueryKey } from "./useDatasets";

/**
 * The `['reviews', id]` key *prefix* shared by every
 * `['reviews', id, params]` page/filter variant (see `reviewsQueryKey`).
 * Invalidating this prefix invalidates all of them at once.
 */
function reviewsPrefix(id: string): readonly unknown[] {
  return ["reviews", id];
}

/**
 * Watch a dataset's `active_version` and, when it changes to a different
 * defined version, invalidate the detail, reviews, and snapshot queries so the
 * new version's data loads in place.
 *
 * Mount this on the detail page with the id it is showing and the
 * `active_version` from its {@link useDataset} result; it returns nothing and
 * only drives cache invalidations.
 *
 * @param id - the dataset id the page is showing, or null before it is known.
 * @param activeVersion - the current `active_version` from the detail query
 *   (null while there is no active version yet).
 */
export function useActiveVersionRefetch(
  id: string | null,
  activeVersion: number | null | undefined,
): void {
  const client = useQueryClient();
  // The last active_version we observed for this id. Keyed by id so switching
  // the page to a different dataset re-primes rather than firing a false change.
  const prevRef = useRef<{ id: string | null; version: number | null | undefined }>({
    id,
    version: activeVersion,
  });

  useEffect(() => {
    const prev = prevRef.current;

    // The page switched to a different dataset: re-prime, don't invalidate.
    if (prev.id !== id) {
      prevRef.current = { id, version: activeVersion };
      return;
    }

    const previousVersion = prev.version;
    prevRef.current = { id, version: activeVersion };

    // Only act on a transition between two *defined* versions on the same id.
    if (
      id != null &&
      previousVersion != null &&
      activeVersion != null &&
      previousVersion !== activeVersion
    ) {
      void client.invalidateQueries({ queryKey: datasetQueryKey(id) });
      // Prefix match: every ['reviews', id, params] page/filter variant.
      void client.invalidateQueries({ queryKey: reviewsPrefix(id) });
      void client.invalidateQueries({ queryKey: snapshotQueryKey(id) });
    }
  }, [client, id, activeVersion]);
}
