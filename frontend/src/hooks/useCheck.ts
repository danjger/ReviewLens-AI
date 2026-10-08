/**
 * `useCheck` — drive one Check Session from the URL tab.
 *
 * Responsibilities (dataset-ingestion task 9.1):
 * - Start a Check with {@link createCheck} and expose the create mutation so the
 *   Check button can block a second submission while one is running
 *   (Requirement 1.5).
 * - Poll `GET /ingest/checks/{id}` while any item is still non-terminal, using
 *   TanStack Query's `refetchInterval` (polling is the design's documented
 *   fallback for the real-time `check.updated` events; the WebSocket push is a
 *   later spec). Polling stops automatically once every item is terminal.
 * - Expose a per-item retry mutation ({@link retryCheckItem}) that resets an
 *   `error`/timed-out item and re-triggers polling (Requirement 3.10).
 * - Surface a `429` rate-limit error (with `retryAfterSeconds`) distinctly so
 *   the panel can say when checks can resume (Requirement 1.6).
 *
 * The current `check_id` is owned by the caller (so it can be mirrored into the
 * `?check=` query string); this hook takes `checkId` as input and reports a new
 * one via `onCheckCreated`.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
} from "@tanstack/react-query";
import { useCallback } from "react";

import {
  ApiError,
  createCheck,
  getCheck,
  isTerminal,
  retryCheckItem,
  type CheckItem,
  type CheckSession,
} from "../api/ingest";
import { checkRefetchInterval } from "./realtimePolling";

/** Query key for a single Check Session. */
export function checkQueryKey(checkId: string | null): readonly unknown[] {
  return ["check", checkId];
}

/** What {@link useCheck} returns. */
export interface UseCheckResult {
  /** The current session (items + metadata), or undefined before first load. */
  session: CheckSession | undefined;
  /** The current items (empty until the first create/poll resolves). */
  items: CheckItem[];
  /** True while a Check is being created or any item is still in flight. */
  isRunning: boolean;
  /** The create-check mutation (call `.mutate(urls)` from the Check button). */
  create: UseMutationResult<CheckSession, Error, string[]>;
  /** Re-queue one item that errored or timed out. */
  retryItem: (itemId: string) => void;
  /** True while a retry request is in flight. */
  isRetrying: boolean;
  /** The rate-limit error, if the last create/retry hit a 429. */
  rateLimit: ApiError | null;
  /** A non-rate-limit error from create/poll, if any. */
  error: Error | null;
}

/** Pick the most relevant rate-limit error from create/retry state. */
function rateLimitOf(...errors: (Error | null)[]): ApiError | null {
  for (const err of errors) {
    if (err instanceof ApiError && err.isRateLimited) return err;
  }
  return null;
}

/**
 * Drive the Check identified by `checkId`.
 *
 * @param checkId - the current check id, or `null` when none is active.
 * @param onCheckCreated - called with the new id right after a Check is
 *   created, so the caller can persist it (e.g. into `?check=`).
 */
export function useCheck(
  checkId: string | null,
  onCheckCreated?: (checkId: string) => void,
): UseCheckResult {
  const queryClient = useQueryClient();

  const query = useQuery<CheckSession>({
    queryKey: checkQueryKey(checkId),
    queryFn: () => getCheck(checkId as string),
    enabled: checkId != null,
    // Poll while any item is still non-terminal; stop once all are terminal.
    // The cadence is the shared real-time backstop (`checkRefetchInterval`,
    // 2 s) so the Check query and the Library's active-check polling agree.
    refetchInterval: (q) => checkRefetchInterval(q.state.data),
    // A 404 means the session expired; don't hammer it with retries.
    retry: (failureCount, err) =>
      !(err instanceof ApiError) && failureCount < 2,
  });

  const create = useMutation<CheckSession, Error, string[]>({
    mutationFn: async (urls: string[]) => {
      const created = await createCheck(urls);
      // Seed the query cache so items render immediately, before the first poll.
      const session: CheckSession = {
        check_id: created.check_id,
        created_at: new Date().toISOString(),
        origin: "new",
        items: created.items,
      };
      return session;
    },
    onSuccess: (session) => {
      queryClient.setQueryData(checkQueryKey(session.check_id), session);
      onCheckCreated?.(session.check_id);
    },
  });

  const retry = useMutation<void, Error, string>({
    mutationFn: (itemId: string) => retryCheckItem(checkId as string, itemId),
    onSuccess: () => {
      // Re-enable polling by invalidating the session so it refetches.
      void queryClient.invalidateQueries({ queryKey: checkQueryKey(checkId) });
    },
  });

  const retryItem = useCallback(
    (itemId: string) => {
      if (checkId == null) return;
      retry.mutate(itemId);
    },
    [checkId, retry],
  );

  const items = query.data?.items ?? [];
  const anyInFlight = items.length > 0 && !items.every(isTerminal);
  const isRunning = create.isPending || anyInFlight;

  return {
    session: query.data,
    items,
    isRunning,
    create,
    retryItem,
    isRetrying: retry.isPending,
    rateLimit: rateLimitOf(create.error, retry.error),
    error:
      create.error instanceof ApiError && create.error.isRateLimited
        ? null
        : (create.error ?? query.error ?? null),
  };
}
