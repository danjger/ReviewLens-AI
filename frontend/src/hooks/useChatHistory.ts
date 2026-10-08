/**
 * useChatHistory — TanStack Query hook for the shared Q&A history timeline
 * (guardrailed-chat task 6.1, Requirements 5.2, 5.3).
 *
 * The history endpoint pages **backward** by a single timestamp cursor
 * (`app/chat/history.py`): the newest page is fetched with no `before`, and
 * each older page is fetched by passing the previous page's `next_before`
 * (design "Endpoints"; `src/api/chat.ts`). That maps directly onto
 * `useInfiniteQuery`:
 *
 *   - the FIRST page (initialPageParam `null`) is the newest page — the end of
 *     the timeline the pane opens scrolled to (Requirement 5.2);
 *   - `getNextPageParam` returns each page's `next_before`, so `fetchNextPage()`
 *     loads the immediately OLDER page of 20 as the analyst scrolls up
 *     (Requirement 5.3). "Next" here means "next older", matching the pane's
 *     top-of-list "earlier" loader.
 *
 * Each page is already oldest-first within itself, and successive pages are
 * progressively older, so the natural reading order (oldest → newest) is the
 * pages in REVERSE fetch order, each page in its own order. {@link flattenHistory}
 * does exactly that flattening for the pane.
 *
 * Hook style mirrors {@link useDatasets}: a stable literal query key, the typed
 * `src/api/chat.ts` client as the query fn, and a `datasetId`-gated `enabled`.
 * The key is exported as {@link chatHistoryQueryKey} so later subtasks (6.8's
 * realtime refetch, 6.4's merge-on-save) target the exact cache entry.
 */
import {
  useInfiniteQuery,
  type InfiniteData,
  type UseInfiniteQueryResult,
} from "@tanstack/react-query";

import {
  HISTORY_PAGE_SIZE,
  getChatHistory,
  type HistoryPage,
  type TimelineItem,
} from "../api/chat";

/**
 * Literal query key for a dataset's chat history timeline.
 *
 * Keyed under `['chat-history', id]`. Later subtasks (6.8 realtime refetch)
 * invalidate or refetch this exact key when a `dataset.status.changed` or
 * `chat.exchange.saved` event lands, so markers appear and Exchanges merge
 * without a reload. A single source of truth, imported rather than re-declared.
 */
export function chatHistoryQueryKey(id: string): readonly unknown[] {
  return ["chat-history", id];
}

/**
 * Query a dataset's shared history timeline, backward-paged by the `before`
 * cursor in pages of {@link HISTORY_PAGE_SIZE} (20).
 *
 * The first page is the newest (end of timeline); `fetchNextPage()` loads older
 * pages. `enabled` is gated on `id` so the detail page can mount the pane before
 * the id is known. Returns the standard infinite-query result; the pane reads
 * `data.pages`, `hasNextPage`, `fetchNextPage`, and `isFetchingNextPage`.
 *
 * @param id - the dataset id, or null before it is known.
 */
export function useChatHistory(
  id: string | null,
): UseInfiniteQueryResult<InfiniteData<HistoryPage, string | null>, Error> {
  return useInfiniteQuery<
    HistoryPage,
    Error,
    InfiniteData<HistoryPage, string | null>,
    readonly unknown[],
    string | null
  >({
    queryKey: chatHistoryQueryKey(id ?? "__none__"),
    queryFn: ({ pageParam }) =>
      getChatHistory(id as string, {
        before: pageParam ?? undefined,
        limit: HISTORY_PAGE_SIZE,
      }),
    // No `before` → the newest page (the end of the timeline, Requirement 5.2).
    initialPageParam: null,
    // The next page to fetch is the immediately OLDER one, addressed by this
    // page's `next_before`; null when this page is the start of the timeline.
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_before : undefined),
    enabled: id != null,
  });
}

/**
 * Flatten the infinite-query pages into a single oldest-first timeline.
 *
 * Pages are fetched newest-first (page 0 is the newest), each page already
 * oldest-first within itself. Reading the whole timeline oldest → newest is
 * therefore the pages in reverse order, each page kept in its own order:
 *
 *   [...olderPage.items, ..., ...newestPage.items]
 *
 * so the result is strictly ascending in time and ready to render top (oldest)
 * to bottom (newest), with the "earlier" loader at the top.
 */
export function flattenHistory(
  data: InfiniteData<HistoryPage, string | null> | undefined,
): TimelineItem[] {
  if (data == null) return [];
  const items: TimelineItem[] = [];
  // Pages are newest-first; prepend older pages so the result is oldest-first.
  for (let i = data.pages.length - 1; i >= 0; i -= 1) {
    items.push(...data.pages[i].items);
  }
  return items;
}
