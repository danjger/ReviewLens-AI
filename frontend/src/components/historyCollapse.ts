/**
 * Pure helpers for the history toolbar (guardrailed-chat task 6.7,
 * Requirement 9.5).
 *
 * The toolbar does two things, both of which reduce to pure functions over the
 * flattened, oldest-first timeline ({@link TimelineItem}[]) that
 * {@link HistoryPane} already builds:
 *
 * 1. **Jump to refresh** — scroll the pane to the previous or next
 *    {@link RefreshMarker} relative to the row currently in view. That is a
 *    navigation over the *indices of marker rows* in the flattened timeline:
 *    {@link markerIndices} finds them, and {@link findPrevMarkerIndex} /
 *    {@link findNextMarkerIndex} pick the marker index to scroll to from the
 *    current index. The pane feeds the result to the virtualizer's
 *    `scrollToIndex` (design "Frontend (ChatPanel)").
 *
 * 2. **Collapse earlier versions** — fold every Exchange from a data version
 *    older than the active version into one summarising row per version
 *    ("{n} questions on v{k} — show"), expandable, while the refresh markers
 *    stay in place and current-version Exchanges stay expanded (Requirement
 *    9.5). {@link buildDisplayRows} turns the timeline into the list of rows to
 *    render given the collapse toggle and the set of individually-expanded
 *    versions.
 *
 * "Earlier version" is read straight off the history layer's `is_stale` flag:
 * an Exchange is stale exactly when its `data_version` is older than the
 * dataset's `active_version` (`src/api/chat.ts`; Requirement 9.4), so collapse
 * needs no separate active-version input — the stale Exchanges are the ones to
 * fold, and the per-Exchange `data_version` groups them by version.
 *
 * Keeping this logic pure (no React, no DOM) makes the fold/navigation
 * unit-testable in isolation (task 6.7's focused tests); the component wiring
 * in {@link HistoryPane} stays a thin shell over these functions.
 */
import { isExchange, isRefreshMarker, type TimelineItem } from "../api/chat";

/**
 * The indices (into the flattened, oldest-first timeline) of every refresh
 * marker row, ascending. These are the stops the "jump to refresh" buttons move
 * between, and they key directly onto the virtualizer's `scrollToIndex`.
 */
export function markerIndices(items: readonly DisplayRow[]): number[] {
  const indices: number[] = [];
  for (let i = 0; i < items.length; i += 1) {
    const row = items[i];
    if (!isCollapsedVersionRow(row) && isRefreshMarker(row)) indices.push(i);
  }
  return indices;
}

/**
 * The index of the first marker strictly *after* `currentIndex`, or `null` when
 * there is none (the view is at or past the last marker). `markerIdxs` must be
 * ascending (as {@link markerIndices} returns). A `currentIndex` of `-1` (no
 * known position yet) returns the first marker, so a first "next" press jumps to
 * the earliest marker.
 */
export function findNextMarkerIndex(
  markerIdxs: readonly number[],
  currentIndex: number,
): number | null {
  for (const idx of markerIdxs) {
    if (idx > currentIndex) return idx;
  }
  return null;
}

/**
 * The index of the last marker strictly *before* `currentIndex`, or `null` when
 * there is none (the view is at or before the first marker). `markerIdxs` must
 * be ascending.
 */
export function findPrevMarkerIndex(
  markerIdxs: readonly number[],
  currentIndex: number,
): number | null {
  let found: number | null = null;
  for (const idx of markerIdxs) {
    if (idx < currentIndex) found = idx;
    else break;
  }
  return found;
}

/** A folded stand-in for one earlier version's collapsed Exchanges. */
export interface CollapsedVersionRow {
  /** Discriminator for a collapsed summary row. */
  type: "collapsed_version";
  /** The (earlier) data version whose Exchanges are folded into this row. */
  version: number;
  /** How many Exchanges this row stands in for (exposed as `data-count`). */
  count: number;
}

/** One row to render: a passthrough timeline item, or a collapsed summary row. */
export type DisplayRow = TimelineItem | CollapsedVersionRow;

/** True when a display row is a collapsed summary row (narrowing helper). */
export function isCollapsedVersionRow(row: DisplayRow): row is CollapsedVersionRow {
  return (row as CollapsedVersionRow).type === "collapsed_version";
}

/** Params for {@link buildDisplayRows}. */
export interface BuildDisplayRowsParams {
  /** The flattened, oldest-first timeline. */
  items: readonly TimelineItem[];
  /** Whether "collapse earlier versions" is on. */
  collapsed: boolean;
  /** Versions the analyst has individually expanded back open while collapsed. */
  expandedVersions: ReadonlySet<number>;
}

/**
 * Build the rows {@link HistoryPane} renders, applying the collapse toggle.
 *
 * When `collapsed` is false the timeline is returned unchanged. When true,
 * every *stale* Exchange (an earlier version's Exchange) is folded into a single
 * {@link CollapsedVersionRow} per version — placed where that version's first
 * stale Exchange sat — unless the version is in `expandedVersions`, in which
 * case its Exchanges are shown as normal. Refresh markers and current-version
 * (non-stale) Exchanges always pass through untouched (Requirement 9.5).
 *
 * The fold is per *version*, not per contiguous run: all of v2's stale
 * Exchanges collapse into one "v2" row even if a marker sits between them, so
 * the analyst gets exactly one summary row per earlier version. The row keeps
 * its original timeline position (the spot of that version's first Exchange),
 * so the surrounding markers and current-version Exchanges stay in order.
 */
export function buildDisplayRows({
  items,
  collapsed,
  expandedVersions,
}: BuildDisplayRowsParams): DisplayRow[] {
  if (!collapsed) return [...items];

  // Count the stale Exchanges per version so each summary row can report how
  // many it stands in for, and remember which version was seen first where.
  const counts = new Map<number, number>();
  for (const item of items) {
    if (isExchange(item) && item.is_stale) {
      counts.set(item.data_version, (counts.get(item.data_version) ?? 0) + 1);
    }
  }

  const emitted = new Set<number>();
  const rows: DisplayRow[] = [];
  for (const item of items) {
    const staleExchange = isExchange(item) && item.is_stale;
    if (!staleExchange) {
      // Markers and current-version Exchanges always pass through.
      rows.push(item);
      continue;
    }
    const version = (item as Extract<TimelineItem, { type: "exchange" }>).data_version;
    if (expandedVersions.has(version)) {
      // This earlier version was expanded back open: show its Exchanges.
      rows.push(item);
      continue;
    }
    // Collapsed: emit one summary row at the position of the version's first
    // stale Exchange, and drop the rest of that version's stale Exchanges.
    if (!emitted.has(version)) {
      emitted.add(version);
      rows.push({
        type: "collapsed_version",
        version,
        count: counts.get(version) ?? 0,
      });
    }
  }
  return rows;
}

/**
 * Whether any earlier-version (stale) Exchange exists in the timeline — i.e.
 * whether "collapse earlier versions" has anything to fold. The toggle is
 * disabled when this is false (nothing to collapse).
 */
export function hasEarlierVersions(items: readonly TimelineItem[]): boolean {
  return items.some((item) => isExchange(item) && item.is_stale);
}
