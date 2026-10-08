/**
 * HistoryPane — the virtualized Q&A history timeline (guardrailed-chat task
 * 6.1, Requirements 5.2, 5.3, 5.4).
 *
 * A scrollable, virtualized timeline of Exchanges and refresh-marker rows,
 * oldest at the top and newest at the bottom (design "Frontend (ChatPanel)").
 * It reads the shared history through {@link useChatHistory} and renders the
 * flattened, oldest-first timeline.
 *
 * **Opens scrolled to the latest Exchange (Requirement 5.2).** On the first
 * load the pane jumps to the bottom (the newest item), and it auto-scrolls to
 * the bottom again whenever a newer item is appended — so a new Exchange (from
 * this tab on `done`, task 6.4, or another visitor via `chat.exchange.saved`)
 * lands in view. If the analyst has scrolled up to read earlier history, a new
 * append does not yank them back down.
 *
 * **Loads earlier history on scroll (Requirement 5.3).** An "earlier" loader
 * sits at the top; reaching the top triggers `fetchNextPage()`, which loads the
 * next older page of 20 ({@link useChatHistory} pages backward by the `before`
 * cursor). The scroll position is preserved across a prepend so the view does
 * not jump when older rows are inserted above the current one.
 *
 * **Version labels (Requirements 5.4, 9.4).** A stale Exchange (its
 * `data_version` older than the dataset's `active_version` — the backend's
 * `is_stale` flag) carries the `data-stale` marker and reduced-emphasis styling
 * and renders the {@link StaleExchangeChip} ("Based on earlier data (v{n},
 * {date})" with an explanatory tooltip, task 6.6).
 *
 * **Virtualization.** Uses `@tanstack/react-virtual` (`useVirtualizer`), chosen
 * because the project already uses TanStack Query — one ecosystem, dynamic row
 * measurement (Exchanges and markers differ in height), and an exact pinned
 * version (`3.14.13`). Rows are absolutely positioned inside a spacer sized to
 * the measured total; each row reports its height via `measureElement`.
 *
 * The row components are: the Exchange row (with citation chips/popovers and
 * the decline tag, 6.2) and {@link RefreshMarker} (6.6). A stale Exchange also
 * renders the {@link StaleExchangeChip} (6.6). Selectors are data-testid /
 * data-* only; tests never assert on counts/dates text (testing.md).
 */
import { useVirtualizer } from "@tanstack/react-virtual";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { isExchange, type ChatExchange } from "../api/chat";
import { useChatHistory, flattenHistory } from "../hooks/useChatHistory";
import CitationChip from "./CitationChip";
import DeclineTag from "./DeclineTag";
import HistoryToolbar from "./HistoryToolbar";
import RefreshMarker from "./RefreshMarker";
import StaleExchangeChip from "./StaleExchangeChip";
import {
  buildDisplayRows,
  findNextMarkerIndex,
  findPrevMarkerIndex,
  hasEarlierVersions,
  isCollapsedVersionRow,
  markerIndices,
  type CollapsedVersionRow,
  type DisplayRow,
} from "./historyCollapse";

export interface HistoryPaneProps {
  /** The dataset id whose shared history to show, or null before it is known. */
  datasetId: string | null;
  /** Estimated row height (px) for the virtualizer's first pass (overridable). */
  estimateRowHeight?: number;
}

/** Default estimated row height before a row measures itself. */
const DEFAULT_ESTIMATE_ROW_HEIGHT = 96;

/**
 * A stable key for a display row: Exchange id, marker version, or the folded
 * collapsed-version row's version. Keys are prefixed by kind so a collapsed
 * "v2" row and the "v2" marker never collide.
 */
function rowKey(row: DisplayRow): string {
  if (isCollapsedVersionRow(row)) return `collapsed:${row.version}`;
  return isExchange(row) ? `exchange:${row.id}` : `marker:${row.version}`;
}

/**
 * One Exchange row: question, answer, citation chips, a decline tag when the
 * answer was declined, and (when stale) a minimal version label.
 *
 * **Citation chips (task 6.2, Requirement 2.2).** Below the answer, each id in
 * the Exchange's `citations` list renders as a {@link CitationChip} whose
 * popover reads the review text/rating/date from the snippet **saved with the
 * Exchange** (`citation_snippets[id]`), not a re-lookup — so a stale Exchange's
 * citations still show the right review after a refresh. The raw inline
 * `[r_xxxx]` tokens stay readable in the answer text; the chips are an
 * interactive row derived from `citations`.
 *
 * **Decline tag (task 6.2, Requirement 3.4).** When `scope === "declined"` the
 * row shows the subtle {@link DeclineTag} ("Outside dataset scope").
 *
 * **Stale styling (task 6.6, Requirement 9.4).** When `is_stale` the row keeps
 * its `data-stale` marker and reduced-emphasis styling and renders the
 * {@link StaleExchangeChip} ("Based on earlier data (v{n}, {date})" + tooltip).
 * The citation chips above it keep working — they read from the Exchange's
 * saved snippets, so a stale Exchange's popovers still show the right review.
 */
function ExchangeRow({ exchange }: { exchange: ChatExchange }) {
  return (
    <article
      className="history-pane__exchange"
      data-testid="history-exchange"
      data-exchange-id={exchange.id}
      data-stale={exchange.is_stale}
      data-scope={exchange.scope}
      data-version={exchange.data_version}
    >
      <p className="history-pane__question" data-testid="history-question">
        {exchange.question}
      </p>
      <p className="history-pane__answer" data-testid="history-answer">
        {exchange.answer}
      </p>
      {exchange.scope === "declined" && (
        <DeclineTag category={exchange.scope_category} />
      )}
      {exchange.citations.length > 0 && (
        <p className="history-pane__citations" data-testid="history-citations">
          {exchange.citations.map((id) => (
            <CitationChip key={id} id={id} snippet={exchange.citation_snippets[id]} />
          ))}
        </p>
      )}
      {exchange.is_stale && (
        <StaleExchangeChip version={exchange.data_version} askedAt={exchange.asked_at} />
      )}
    </article>
  );
}

/**
 * A folded summary row standing in for one earlier version's collapsed
 * Exchanges (task 6.7, Requirement 9.5). It shows "{n} questions on v{k} —
 * show" and, pressed, expands that version's Exchanges back into the timeline.
 *
 * The expand control is a real `<button>` with `aria-expanded={false}` (the
 * version is folded) and an accessible label; the count is exposed as
 * `data-count` so tests assert the fold without matching the formatted copy
 * (testing.md).
 */
function CollapsedVersionSummary({
  row,
  onExpand,
}: {
  row: CollapsedVersionRow;
  onExpand: (version: number) => void;
}) {
  return (
    <div
      className="history-pane__collapsed-version"
      data-testid="history-collapsed-version"
      data-version={row.version}
      data-count={row.count}
      role="separator"
      aria-label={`Collapsed questions on version ${row.version}`}
    >
      <span className="history-pane__collapsed-text">
        {row.count} {row.count === 1 ? "question" : "questions"} on v{row.version}
      </span>
      <button
        type="button"
        className="history-pane__collapsed-expand"
        data-testid="history-collapsed-expand"
        data-version={row.version}
        onClick={() => onExpand(row.version)}
        aria-expanded={false}
        aria-label={`Show questions on version ${row.version}`}
      >
        show
      </button>
    </div>
  );
}

export default function HistoryPane({
  datasetId,
  estimateRowHeight = DEFAULT_ESTIMATE_ROW_HEIGHT,
}: HistoryPaneProps) {
  const query = useChatHistory(datasetId);
  const items = flattenHistory(query.data);

  const scrollRef = useRef<HTMLDivElement | null>(null);

  // ── Collapse-earlier-versions state (task 6.7, Requirement 9.5) ────────────
  // `collapsed` is the toolbar toggle; `expandedVersions` holds the earlier
  // versions the analyst expanded back open individually while collapsed.
  const [collapsed, setCollapsed] = useState(false);
  const [expandedVersions, setExpandedVersions] = useState<ReadonlySet<number>>(
    () => new Set<number>(),
  );

  // The rows actually rendered: the timeline unchanged, or with earlier
  // versions folded into one summary row each when `collapsed` is on.
  const rows = useMemo(
    () => buildDisplayRows({ items, collapsed, expandedVersions }),
    [items, collapsed, expandedVersions],
  );

  // The display-row indices of refresh markers, for jump navigation, and
  // whether there is anything to jump to / collapse (drives disabled buttons).
  // Collapsing keeps markers, so this is computed over the rendered rows so the
  // indices line up with the virtualizer's `scrollToIndex`.
  const markerIdxs = useMemo(() => markerIndices(rows), [rows]);
  const hasMarkers = markerIdxs.length > 0;
  const canCollapse = useMemo(() => hasEarlierVersions(items), [items]);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => estimateRowHeight,
    overscan: 8,
    getItemKey: (index) => rowKey(rows[index]),
  });

  // ── Auto-scroll to the newest item (Requirement 5.2) ───────────────────────
  // We detect "a newer item was appended at the bottom" vs "older items were
  // prepended at the top" by comparing the id of the LAST (newest) item across
  // renders. We only auto-scroll to the bottom on the first populated render
  // and when the newest item changes, and only when the user is already near
  // the bottom (so reading earlier history isn't interrupted).
  const lastNewestKeyRef = useRef<string | null>(null);
  const didInitialScrollRef = useRef(false);

  const newestKey = items.length > 0 ? rowKey(items[items.length - 1]) : null;

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el == null || items.length === 0) return;

    const prevNewest = lastNewestKeyRef.current;
    const isFirstPopulated = !didInitialScrollRef.current;
    const newestChanged = prevNewest !== null && prevNewest !== newestKey;

    // Distance from the bottom; "near bottom" tolerates sub-pixel rounding and
    // small row-measurement adjustments.
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const nearBottom = distanceFromBottom <= estimateRowHeight * 2;

    if (isFirstPopulated || (newestChanged && nearBottom)) {
      // Jump to the newest row (bottom). Index-based so it works with
      // virtualized, not-yet-measured rows below the current window. The target
      // is the last *display* row, which the newest item always ends up as
      // (collapsing never folds the current-version newest Exchange).
      virtualizer.scrollToIndex(rows.length - 1, { align: "end" });
      didInitialScrollRef.current = true;
    }

    lastNewestKeyRef.current = newestKey;
  }, [newestKey, items.length, rows.length, estimateRowHeight, virtualizer]);

  // ── Jump-to-refresh navigation (task 6.7, Requirement 9.5) ─────────────────
  // Track the row index currently at the top of the viewport so "previous" and
  // "next" are relative to what the analyst is looking at. It is updated on
  // scroll from the virtualizer's first visible item.
  const currentIndexRef = useRef<number>(-1);

  const onJumpPrev = useCallback(() => {
    const target = findPrevMarkerIndex(markerIdxs, currentIndexRef.current);
    if (target != null) {
      virtualizer.scrollToIndex(target, { align: "start" });
      currentIndexRef.current = target;
    }
  }, [markerIdxs, virtualizer]);

  const onJumpNext = useCallback(() => {
    const target = findNextMarkerIndex(markerIdxs, currentIndexRef.current);
    if (target != null) {
      virtualizer.scrollToIndex(target, { align: "start" });
      currentIndexRef.current = target;
    }
  }, [markerIdxs, virtualizer]);

  const onToggleCollapse = useCallback(() => {
    setCollapsed((prev) => {
      // Leaving collapse clears any per-version expansions, so re-collapsing
      // starts folded again — a clean, predictable toggle.
      if (prev) setExpandedVersions(new Set<number>());
      return !prev;
    });
  }, []);

  const onExpandVersion = useCallback((version: number) => {
    setExpandedVersions((prev) => {
      const next = new Set(prev);
      next.add(version);
      return next;
    });
  }, []);

  // ── Load earlier history when scrolled to the top (Requirement 5.3) ─────────
  // Preserve the scroll position across a prepend: record the scrollHeight
  // before fetching the older page, and after it lands restore scrollTop by the
  // height delta so the row the analyst was reading stays put.
  const prependAnchorRef = useRef<number | null>(null);

  const onScroll = (): void => {
    const el = scrollRef.current;
    if (el == null) return;
    // Remember the top-most visible row so jump-to-refresh is relative to the
    // current view.
    const visible = virtualizer.getVirtualItems();
    if (visible.length > 0) currentIndexRef.current = visible[0].index;
    if (el.scrollTop <= estimateRowHeight && query.hasNextPage && !query.isFetchingNextPage) {
      prependAnchorRef.current = el.scrollHeight;
      void query.fetchNextPage();
    }
  };

  // After an older page is prepended, restore the scroll offset by the growth
  // in scrollHeight so the viewport does not jump to the new top.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el == null) return;
    const anchor = prependAnchorRef.current;
    if (anchor != null && !query.isFetchingNextPage) {
      const delta = el.scrollHeight - anchor;
      if (delta > 0) el.scrollTop = el.scrollTop + delta;
      prependAnchorRef.current = null;
    }
  }, [items.length, query.isFetchingNextPage]);

  // Reset the auto-scroll bookkeeping when the dataset changes, so the pane
  // opens scrolled to the newest item of the newly selected dataset.
  useEffect(() => {
    didInitialScrollRef.current = false;
    lastNewestKeyRef.current = null;
    prependAnchorRef.current = null;
    currentIndexRef.current = -1;
    setCollapsed(false);
    setExpandedVersions(new Set<number>());
  }, [datasetId]);

  const virtualItems = virtualizer.getVirtualItems();

  return (
    <section className="history-pane" data-testid="history-pane" aria-label="Question history">
      {/* Toolbar: jump between refresh markers and collapse earlier versions
          (task 6.7, Requirement 9.5). Shown once there is history to act on. */}
      {items.length > 0 && (
        <HistoryToolbar
          onJumpPrev={onJumpPrev}
          onJumpNext={onJumpNext}
          hasMarkers={hasMarkers}
          collapsed={collapsed}
          onToggleCollapse={onToggleCollapse}
          canCollapse={canCollapse}
        />
      )}
      <div
        className="history-pane__scroll"
        data-testid="history-scroll"
        ref={scrollRef}
        onScroll={onScroll}
        role="log"
        aria-live="polite"
      >
        {/* The top "earlier" loader (Requirement 5.3). Visible only while there
            are older pages to load; shows a loading state during a fetch. */}
        {query.hasNextPage && (
          <div
            className="history-pane__earlier"
            data-testid="history-earlier"
            data-loading={query.isFetchingNextPage}
            aria-hidden={!query.isFetchingNextPage}
          >
            {query.isFetchingNextPage ? "Loading earlier history…" : "Scroll up for earlier history"}
          </div>
        )}

        {query.isError && (
          <p className="history-pane__error" data-testid="history-error" role="alert">
            {query.error.message}
          </p>
        )}

        {!query.isError && !query.isPending && items.length === 0 && (
          <p className="history-pane__empty" data-testid="history-empty" role="status">
            No questions yet.
          </p>
        )}

        {/* Virtualized rows: a spacer sized to the measured total, with each
            visible row absolutely positioned and self-measuring. */}
        <div
          className="history-pane__sizer"
          data-testid="history-sizer"
          style={{ height: virtualizer.getTotalSize(), position: "relative", width: "100%" }}
        >
          {virtualItems.map((virtualRow) => {
            const row = rows[virtualRow.index];
            return (
              <div
                key={virtualRow.key}
                data-testid="history-row"
                data-index={virtualRow.index}
                ref={virtualizer.measureElement}
                data-virtual-index={virtualRow.index}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  transform: `translateY(${virtualRow.start}px)`,
                }}
              >
                {isCollapsedVersionRow(row) ? (
                  <CollapsedVersionSummary row={row} onExpand={onExpandVersion} />
                ) : isExchange(row) ? (
                  <ExchangeRow exchange={row} />
                ) : (
                  <RefreshMarker marker={row} />
                )}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
