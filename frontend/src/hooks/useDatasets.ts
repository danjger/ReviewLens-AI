/**
 * useDatasets / useDataset — TanStack Query hooks for the Dataset Library
 * (dataset-library task 6).
 *
 * CRITICAL — query keys: these MUST key off the exact literal keys the task 5
 * real-time reducers patch, or live updates won't land on the rendered data:
 *   - the list on `['datasets']`      (`DATASETS_QUERY_KEY`)
 *   - the detail on `['dataset', id]` (`datasetQueryKey(id)`)
 * We import those from `realtimeReducers` rather than re-declaring the literals,
 * so there is a single source of truth and the push reducers patch the same
 * cache entry this hook reads.
 *
 * The list adopts the shared polling backstop `listRefetchInterval` from task 5
 * as its `refetchInterval`, so when the WebSocket is down the list still
 * refreshes every 10 s while any row is `requested`/`processing`.
 *
 * The list key is held constant at `['datasets']` across sort/search/archived
 * so the push reducers (which patch `['datasets']`) always hit it. The filter
 * params are sent to the server (which does the exact filtering, design
 * Property 3) and change what the single cached list contains; a param change
 * refetches that same key rather than branching into a per-param cache entry.
 */
import { useEffect } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import {
  archiveDataset,
  confirmRefresh,
  getDataset,
  getSnapshotUrl,
  listDatasets,
  listReviews,
  refreshDataset,
  renameDataset,
  restoreDataset,
  type Dataset,
  type DatasetDetail,
  type DatasetSort,
  type ListDatasetsParams,
  type ListReviewsParams,
  type RefreshResponse,
  type ReviewsPage,
  type SnapshotUrl,
} from "../api/datasets";
import { listRefetchInterval } from "./realtimePolling";
import { DATASETS_QUERY_KEY, datasetQueryKey } from "./realtimeReducers";

/** Controls applied to the Tracked Datasets list. */
export interface DatasetListControls {
  /** Show archived datasets instead of the default active list. */
  archived: boolean;
  /** Sort order. */
  sort: DatasetSort;
  /** Case-insensitive name/URL filter text. */
  q: string;
}

/**
 * Query the Tracked Datasets list.
 *
 * Keyed on the literal `['datasets']` (see module note) so the real-time
 * reducers patch the exact entry this returns. Filter params are passed to the
 * server, which returns the already-filtered, already-ordered array.
 */
export function useDatasets(
  controls: DatasetListControls,
): UseQueryResult<Dataset[], Error> {
  const params: ListDatasetsParams = {
    archived: controls.archived,
    sort: controls.sort,
    q: controls.q,
  };
  const result = useQuery<Dataset[], Error>({
    queryKey: [...DATASETS_QUERY_KEY],
    queryFn: () => listDatasets(params),
    // Keep the server-filtered result fresh under param changes.
    refetchInterval: (query) => listRefetchInterval(query.state.data),
  });

  // The query key is held constant at ['datasets'] so the real-time reducers
  // (which patch that literal key) always hit it. But that means a change to
  // the controls (Show archived / sort / search) does NOT change the key, so
  // TanStack would otherwise serve the previously-cached list instead of
  // refetching with the new server-side filter. Refetch explicitly whenever the
  // controls change so the new params take effect (fixes "Show archived" doing
  // nothing).
  const { refetch } = result;
  useEffect(() => {
    void refetch();
    // Refetch on any control change; `refetch` identity is stable per query.
  }, [controls.archived, controls.sort, controls.q, refetch]);

  return result;
}

/** Query one dataset's full record, keyed on `['dataset', id]`. */
export function useDataset(
  id: string | null,
): UseQueryResult<DatasetDetail, Error> {
  return useQuery<DatasetDetail, Error>({
    queryKey: datasetQueryKey(id ?? "__none__"),
    queryFn: () => getDataset(id as string),
    enabled: id != null,
  });
}

/** Literal query key for one dataset's page-snapshot URL. */
export function snapshotQueryKey(id: string): readonly unknown[] {
  return ["snapshot", id];
}

/**
 * Query a dataset's page-snapshot pre-signed URL, keyed on `['snapshot', id]`.
 *
 * The key matches the one the detail page invalidates when a dataset's
 * `active_version` changes (design "Live behavior": invalidate `['snapshot',
 * id]`), so a new version's snapshot reloads in place. The pre-signed URL is
 * short-lived; {@link SnapshotCard} refetches this query once if the image
 * fails to load (an expired URL), which TanStack Query dedupes against the key.
 *
 * Retries are left to the default client config — the card drives the single
 * expired-URL refetch itself via {@link UseQueryResult.refetch}, so this hook
 * does not add its own retry policy.
 */
export function useSnapshotUrl(
  id: string | null,
): UseQueryResult<SnapshotUrl, Error> {
  return useQuery<SnapshotUrl, Error>({
    queryKey: snapshotQueryKey(id ?? "__none__"),
    queryFn: () => getSnapshotUrl(id as string),
    enabled: id != null,
  });
}

/**
 * Literal query key for a page of a dataset's reviews.
 *
 * Keyed under `['reviews', id]` with the filter/page params as a trailing key
 * segment. This matters for live updates: the detail page invalidates
 * `['reviews', id]` (a key *prefix*) when a dataset's `active_version` changes
 * (design "Live behavior"), and TanStack Query's prefix match then invalidates
 * every page/filter combination cached under that id so the new version's
 * reviews reload. Putting the params in a trailing segment (rather than
 * flattening them into the key root) is what makes that prefix invalidation
 * reach them.
 */
export function reviewsQueryKey(
  id: string,
  params: ListReviewsParams,
): readonly unknown[] {
  return ["reviews", id, params];
}

/**
 * Query a page of a dataset's active-version reviews
 * (`GET /datasets/{id}/reviews`), keyed on `['reviews', id, params]`.
 *
 * Server-paginated and server-filtered: `params` (page, page size, rating,
 * sentiment, text search) go straight to the backend, which returns the single
 * already-filtered, already-paginated page. A new filter or page value is a new
 * trailing key segment, so each combination caches independently while all stay
 * under the `['reviews', id]` prefix the active-version-change invalidation
 * targets.
 *
 * `placeholderData` keeps the previous page's rows on screen while the next
 * page/filter loads, so paging and filtering don't flash an empty table.
 */
export function useReviews(
  id: string | null,
  params: ListReviewsParams,
): UseQueryResult<ReviewsPage, Error> {
  return useQuery<ReviewsPage, Error>({
    queryKey: reviewsQueryKey(id ?? "__none__", params),
    queryFn: () => listReviews(id as string, params),
    enabled: id != null,
    placeholderData: (previous) => previous,
  });
}

/** Invalidate the list so it refetches with current controls. */
function useInvalidateList(): () => void {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: [...DATASETS_QUERY_KEY] });
  };
}

/** Arguments for the rename mutation. */
export interface RenameDatasetArgs {
  id: string;
  name: string;
}

/**
 * Rename-a-dataset mutation (`PATCH /datasets/{id}`).
 *
 * Reuses the dataset-library `renameDataset` client. On success it writes the
 * returned record into the `['dataset', id]` detail cache so the inline rename
 * in {@link DatasetHeader} shows the new name without a refetch, and invalidates
 * the `['datasets']` list so the Library row's name updates too.
 */
export function useRenameDataset(): UseMutationResult<
  DatasetDetail,
  Error,
  RenameDatasetArgs
> {
  const client = useQueryClient();
  const invalidate = useInvalidateList();
  return useMutation<DatasetDetail, Error, RenameDatasetArgs>({
    mutationFn: ({ id, name }) => renameDataset(id, name),
    onSuccess: (detail) => {
      client.setQueryData<DatasetDetail>(datasetQueryKey(detail.id), detail);
      invalidate();
    },
  });
}

/** Archive mutation; refetches the list on success so the row drops out. */
export function useArchiveDataset(): UseMutationResult<void, Error, string> {
  const invalidate = useInvalidateList();
  return useMutation<void, Error, string>({
    mutationFn: (id: string) => archiveDataset(id),
    onSuccess: invalidate,
  });
}

/** Restore mutation; refetches the list so the row returns to the default view. */
export function useRestoreDataset(): UseMutationResult<void, Error, string> {
  const invalidate = useInvalidateList();
  return useMutation<void, Error, string>({
    mutationFn: (id: string) => restoreDataset(id),
    onSuccess: invalidate,
  });
}

/**
 * Refresh mutation. Resolves with the `{check_id}` so the caller can track the
 * refresh Check; refetches the list so the row reflects the new in-flight state.
 */
export function useRefreshDataset(): UseMutationResult<
  RefreshResponse,
  Error,
  string
> {
  const invalidate = useInvalidateList();
  return useMutation<RefreshResponse, Error, string>({
    mutationFn: (id: string) => refreshDataset(id),
    onSuccess: invalidate,
  });
}

/** Arguments for the confirm-refresh mutation. */
export interface ConfirmRefreshArgs {
  id: string;
  checkId: string;
}

/** Confirm-a-limited-refresh mutation. */
export function useConfirmRefresh(): UseMutationResult<
  void,
  Error,
  ConfirmRefreshArgs
> {
  const invalidate = useInvalidateList();
  return useMutation<void, Error, ConfirmRefreshArgs>({
    mutationFn: ({ id, checkId }) => confirmRefresh(id, checkId),
    onSuccess: invalidate,
  });
}
