/**
 * Cache patch reducers for real-time frames (dataset-library task 5,
 * Requirements 6.3, 6.4, 6.5).
 *
 * These apply an incoming {@link RealtimeFrame} directly to the TanStack Query
 * cache so the list, the open detail page, and a New Dataset panel update in
 * place, without a refetch. They key off the LITERAL query-key arrays the
 * queries use (design "Frontend"):
 *   - `['datasets']`      — the Tracked Datasets list (populated by task 6)
 *   - `['dataset', id]`   — one dataset's detail record (task 6 / summary)
 *   - `['check', checkId]` — one Check Session (owned by `useCheck`; we reuse
 *                            its `checkQueryKey` so we patch the same entry)
 *
 * Patching a key no query has populated yet is a safe no-op: `setQueryData`
 * only updates an existing entry when given an updater that returns `undefined`
 * for missing data, so a frame for a dataset no query is watching simply does
 * nothing (except the deliberate list-invalidation below).
 *
 * Correctness Property 4 (live updates converge): each reducer is a pure patch
 * keyed on identity, so applying events in any order and then the latest event
 * again yields the latest event's state.
 */

import type { QueryClient } from "@tanstack/react-query";

import type { CheckItem, CheckSession } from "../api/ingest";
import { chatHistoryQueryKey } from "./useChatHistory";
import { checkQueryKey } from "./useCheck";
import {
  isChatExchangeSaved,
  isCheckUpdated,
  isDatasetStatusChanged,
  type ChatExchangeSavedFrame,
  type CheckUpdatedFrame,
  type DatasetRow,
  type DatasetStatusChangedFrame,
  type RealtimeFrame,
} from "./realtimeTypes";

/** Literal query key for the Tracked Datasets list. */
export const DATASETS_QUERY_KEY = ["datasets"] as const;

/** Literal query key for one dataset's detail record. */
export function datasetQueryKey(id: string): readonly unknown[] {
  return ["dataset", id];
}

/**
 * The dataset statuses that affect a refresh marker in the chat timeline, so a
 * `dataset.status.changed` frame carrying one of them must refetch the latest
 * history page (guardrailed-chat task 6.8, Requirements 9.2, 9.7).
 *
 * The design ("Refresh markers") names the lifecycle transitions that move or
 * change a marker: a refresh **started** (`requested`), **completed**
 * (`updated`), or **failed** (`failed`). Requirement 9.2 adds that the pending
 * "Refreshing data…" marker shows while the refresh is `requested` OR
 * `processing`, so a transition into `processing` must also refetch to surface
 * (or keep) that pending marker. We therefore refetch on exactly:
 *
 *   requested → pending marker appears (refresh started)
 *   processing → pending marker (still refreshing; the backend history API
 *                emits the pending marker for both requested and processing)
 *   updated   → pending marker becomes the completed v{n} marker
 *   failed    → pending marker becomes the failed marker
 *
 * Any other status (e.g. the first `requested`→`processing`→`updated` of an
 * initial ingestion still produces markers only for version > 1, which the
 * history API handles; unrelated statuses like `archived` don't change markers)
 * is deliberately excluded so we don't thrash the infinite history query on
 * every status tick. A refresh that fails at the URL check never creates a
 * version row and so never a marker (Requirement 9.8) — but it still passes
 * through `failed`; the refetch is a harmless no-op in that case because the
 * history API returns no new marker, and the query simply reconciles to the
 * same timeline.
 */
const REFRESH_MARKER_STATUSES: ReadonlySet<string> = new Set([
  "requested",
  "processing",
  "updated",
  "failed",
]);

/**
 * Terminal dataset statuses whose live frame settles the row's badge IN PLACE
 * (GitHub issue #10). See {@link applyDatasetStatusChanged} and
 * {@link terminalDisplayState}. Only these terminal transitions carry a derived
 * `display_state` into the patch; non-terminal frames (`requested`/`processing`)
 * are patched without `display_state`, as before, so the row keeps its
 * server-derived in-flight badge until it settles.
 */
const TERMINAL_STATUSES: ReadonlySet<string> = new Set(["updated", "failed"]);

/**
 * Derive the settled list-row `display_state` for a TERMINAL live frame, so the
 * status badge converges live with NO reload (issue #10) WITHOUT refetching the
 * list. A list refetch is deliberately avoided here: the list query is keyed on
 * the constant `['datasets']` with the filter held only in the `useDatasets`
 * queryFn, so an invalidation-triggered refetch races an active Library search
 * filter (re-showing sibling rows, breaking the #12 isolation contract). The
 * reducer's exact-key in-place patch is the safe place to settle the badge.
 *
 * This MUST mirror the server's single source of truth,
 * `backend/app/datasets/library.py::derive_display_state`, for the terminal
 * cases. It is pinned to that mapping by a unit test
 * ({@link file://./realtimeReducers.test.ts} "terminal display_state derivation
 * matches the server") that asserts agreement across the FULL terminal domain —
 * so any future change to the server mapping turns that test red. The terminal
 * cases are TOTAL here:
 *
 *   updated + active_version set   → "ready"
 *   updated + no active_version    → undefined (cannot arise in practice; the
 *                                     server maps it to "processing" as a safe
 *                                     non-terminal fallback, so we delegate it
 *                                     to the polling backstop rather than assert
 *                                     a terminal badge — see the pinning test)
 *   failed  + active_version set   → "ready_refresh_failed"
 *   failed  + no active_version    → "failed"
 *
 * Returns `undefined` when there is nothing safe to settle in place, in which
 * case the caller leaves `display_state` untouched and the polling backstop /
 * reconnect resync reconciles it (same as a non-terminal frame).
 */
export function terminalDisplayState(
  status: string,
  activeVersion: number | null | undefined,
): DatasetRow["display_state"] | undefined {
  const hasActive = activeVersion != null;
  if (status === "updated") return hasActive ? "ready" : undefined;
  if (status === "failed") return hasActive ? "ready_refresh_failed" : "failed";
  return undefined;
}

/**
 * Check-item states the worker won't advance further. A terminal `check.updated`
 * frame must refetch the full Check Session (see {@link applyCheckUpdated}),
 * because the frame carries only {state, verdict} and the terminal in-place
 * patch stops `useCheck`'s poll before the complete verdict detail arrives. Kept
 * in sync with `TERMINAL_STATES` / `isTerminal` in `../api/ingest`.
 */
const TERMINAL_CHECK_STATES: ReadonlySet<string> = new Set([
  "done",
  "invalid",
  "duplicate_in_batch",
  "error",
  "awaiting_confirmation",
  "applied",
]);

/**
 * Build the patch a `dataset.status.changed` frame applies to a dataset row or
 * detail record. Only the fields the push carries are overwritten; everything
 * else on the cached object is preserved.
 *
 * For a NON-TERMINAL frame (`requested`/`processing`), `display_state` is
 * intentionally NOT set: it is derived on the server from `status` +
 * `active_version`, so the row keeps its server-derived in-flight badge until it
 * settles (polling backstop / reconnect resync reconciles it). For a TERMINAL
 * frame (`updated`/`failed`), the settled `display_state` is derived here
 * ({@link terminalDisplayState}) and patched IN PLACE so the status badge
 * converges live with NO reload (GitHub issue #10) — without a list refetch
 * that would race the Library search filter (#12 isolation). The derived value
 * provably matches the server (pinning test). The live `status`,
 * `last_message`, versions, and metrics still update in place so the row reacts
 * immediately.
 */
function datasetPatchFields(frame: DatasetStatusChangedFrame): Partial<DatasetRow> {
  const reviewCount =
    typeof frame.metrics?.review_count === "number"
      ? (frame.metrics.review_count as number)
      : undefined;
  const patch: Partial<DatasetRow> = {
    status: frame.status,
    last_message: frame.message,
    data_version: frame.data_version,
    active_version: frame.active_version,
  };
  if (reviewCount !== undefined) patch.review_count = reviewCount;
  if (TERMINAL_STATUSES.has(frame.status)) {
    const settled = terminalDisplayState(frame.status, frame.active_version);
    if (settled !== undefined) patch.display_state = settled;
  }
  return patch;
}

/**
 * Apply a `dataset.status.changed` frame.
 *
 * - Patches the matching row inside the `['datasets']` list cache.
 * - Patches the `['dataset', id]` detail cache (no-op until it exists).
 * - If the dataset id is NOT present in the `['datasets']` list cache (a
 *   dataset created elsewhere that this tab has never seen), invalidates
 *   `['datasets']` so the list refetches and picks the new row up
 *   (Requirement 6.5, design "Frontend").
 * - For a TERMINAL frame ({@link TERMINAL_STATUSES}: `updated` / `failed`), the
 *   in-place patch above ALSO carries a derived `display_state` (see
 *   {@link datasetPatchFields} / {@link terminalDisplayState}) so the status
 *   badge converges live with NO reload — the live-push convergence fix for
 *   GitHub issue #10. This is done WITHOUT refetching the list, because the list
 *   query holds its filter outside the (constant) query key, so a refetch would
 *   race an active Library search and re-show sibling rows (breaking the #12
 *   isolation contract). The derived badge provably equals what a list refetch
 *   would return (pinned to the server mapping by a unit test). Non-terminal
 *   frames keep the prior behavior of not touching `display_state`.
 * - If the frame's `status` is a refresh-marker lifecycle transition
 *   ({@link REFRESH_MARKER_STATUSES}), invalidates the dataset's chat-history
 *   query (`['chat-history', id]`) so `HistoryPane` refetches the latest page
 *   and the pending/completed/failed `RefreshMarker` appears or changes state
 *   live, without a page reload (guardrailed-chat task 6.8, Requirements 9.2,
 *   9.7). A tab not showing that dataset's history has no such cache entry, so
 *   the invalidation is a harmless no-op there. This converges the same
 *   timeline key as task 6.4's `chat.exchange.saved` refetch.
 */
export function applyDatasetStatusChanged(
  client: QueryClient,
  frame: DatasetStatusChangedFrame,
): void {
  const patch = datasetPatchFields(frame);

  const list = client.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY]);
  const known = Array.isArray(list)
    ? list.some((row) => row.id === frame.dataset_id)
    : false;

  if (Array.isArray(list)) {
    client.setQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY], (rows) =>
      (rows ?? []).map((row) =>
        row.id === frame.dataset_id ? { ...row, ...patch } : row,
      ),
    );
  }

  client.setQueryData<DatasetRow>(datasetQueryKey(frame.dataset_id), (detail) =>
    detail ? { ...detail, ...patch } : detail,
  );

  // Unknown id (and the list is loaded): the row isn't here yet — refetch it.
  // A KNOWN row's terminal badge is settled by the in-place display_state patch
  // above (terminalDisplayState), so NO list refetch is needed — which keeps
  // the Library search filter stable and preserves the #12 isolation contract.
  if (list !== undefined && !known) {
    void client.invalidateQueries({ queryKey: [...DATASETS_QUERY_KEY] });
  }

  // Refresh lifecycle transition: refetch this dataset's chat history so the
  // refresh marker appears/changes state live (task 6.8, Requirements 9.2, 9.7).
  if (REFRESH_MARKER_STATUSES.has(frame.status)) {
    void client.invalidateQueries({
      queryKey: chatHistoryQueryKey(frame.dataset_id),
    });
  }
}

/**
 * Apply a `check.updated` frame by patching the matching item inside the
 * `['check', check_id]` Check Session cache. A browser not showing that check
 * has no such cache entry, so this is a no-op there (Requirement 6.5: "Browsers
 * not showing that Check SHALL ignore it"). We reuse `checkQueryKey` so the
 * patched entry is exactly the one `useCheck` polls.
 */
export function applyCheckUpdated(client: QueryClient, frame: CheckUpdatedFrame): void {
  let patchedSession = false;
  client.setQueryData<CheckSession>(checkQueryKey(frame.check_id), (session) => {
    if (!session) return session;
    patchedSession = true;
    const items: CheckItem[] = session.items.map((item) =>
      item.item_id === frame.item_id
        ? {
            ...item,
            state: frame.state as CheckItem["state"],
            ...(frame.verdict !== null && item.verdict !== null
              ? { verdict: { ...item.verdict, verdict: frame.verdict } }
              : {}),
          }
        : item,
    );
    return { ...session, items };
  });

  // When the pushed item reached a TERMINAL state, also refetch the full Check
  // Session. The `check.updated` frame carries only {state, verdict} — not the
  // full server verdict detail (reasons, sample reviews, and the "already
  // tracked → refresh/duplicate" linkage that renders TrackedNote). The in-place
  // patch above flips `state` to terminal, which stops `useCheck`'s poll
  // (checkRefetchInterval returns false once every item is terminal); without a
  // refetch the complete verdict detail would never land, so a browser that
  // learns the result via the live push (issue #10, now wired locally) would be
  // stuck showing a bare item with no verdict card detail. This refetch makes
  // the check path converge live with NO reload — the same server-derived end
  // state the poll would have fetched, just reached via the live frame. Only on
  // a terminal frame, and only when this tab is actually showing the Check (the
  // session was present to patch), so a non-showing tab stays a no-op
  // (Requirement 6.5). Mirrors the terminal-frame list refetch in
  // applyDatasetStatusChanged.
  if (patchedSession && TERMINAL_CHECK_STATES.has(frame.state)) {
    void client.invalidateQueries({ queryKey: checkQueryKey(frame.check_id) });
  }
}

/**
 * Apply a `chat.exchange.saved` frame (guardrailed-chat task 6.4, Requirement
 * 5.7).
 *
 * The frame carries IDs only (`dataset_id`, `exchange_id`, `key`), not the
 * Exchange content, so there is nothing to patch in place. Instead we
 * invalidate the dataset's chat-history query (`['chat-history', id]`), which
 * refetches the newest page so another visitor's new Exchange — or this tab's
 * save-retry — appears in the shared timeline without a page reload. A tab not
 * showing that dataset's history has no such cache entry, so the invalidation
 * is a harmless no-op there.
 *
 * This is the Exchange-arrival counterpart to task 6.8's `dataset.status.changed`
 * refetch (which keeps refresh markers live); both converge the shared timeline
 * through the same query key.
 */
export function applyChatExchangeSaved(
  client: QueryClient,
  frame: ChatExchangeSavedFrame,
): void {
  void client.invalidateQueries({
    queryKey: chatHistoryQueryKey(frame.dataset_id),
  });
}

/**
 * Dispatch any real-time frame to the right reducer. This is the single entry
 * point the `useRealtime` event bus calls for every incoming frame.
 */
export function applyRealtimeFrame(client: QueryClient, frame: RealtimeFrame): void {
  if (isDatasetStatusChanged(frame)) {
    applyDatasetStatusChanged(client, frame);
  } else if (isCheckUpdated(frame)) {
    applyCheckUpdated(client, frame);
  } else if (isChatExchangeSaved(frame)) {
    applyChatExchangeSaved(client, frame);
  }
}
