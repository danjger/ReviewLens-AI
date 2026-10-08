/**
 * Focused tests for the history-toolbar pure helpers (guardrailed-chat task
 * 6.7, Requirement 9.5).
 *
 * These cover the two bits of logic the toolbar reduces to — collapsing the
 * timeline into one summary row per earlier version, and finding the
 * previous/next refresh-marker index from the current scroll position — in
 * isolation from React/DOM. The comprehensive component coverage is task 6.9.
 */
import { describe, expect, it } from "vitest";

import type { TimelineItem } from "../api/chat";
import { makeChatExchange, makeRefreshMarker } from "../test/fixtures";
import {
  buildDisplayRows,
  findNextMarkerIndex,
  findPrevMarkerIndex,
  hasEarlierVersions,
  isCollapsedVersionRow,
  markerIndices,
} from "./historyCollapse";

describe("markerIndices + jump navigation (Req 9.5)", () => {
  // Timeline: [ex, marker@1, ex, marker@3, ex] → markers at indices 1 and 3.
  const items: TimelineItem[] = [
    makeChatExchange({ id: "a" }),
    makeRefreshMarker({ version: 2 }),
    makeChatExchange({ id: "b" }),
    makeRefreshMarker({ version: 3 }),
    makeChatExchange({ id: "c" }),
  ];

  it("finds every refresh-marker row index, ascending", () => {
    expect(markerIndices(items)).toEqual([1, 3]);
  });

  it("returns no marker indices for a markerless timeline", () => {
    expect(markerIndices([makeChatExchange({ id: "a" })])).toEqual([]);
  });

  it("next marker is the first marker strictly after the current index", () => {
    const markers = markerIndices(items);
    expect(findNextMarkerIndex(markers, 0)).toBe(1);
    expect(findNextMarkerIndex(markers, 1)).toBe(3);
    expect(findNextMarkerIndex(markers, 2)).toBe(3);
  });

  it("next from a position at/after the last marker is null", () => {
    const markers = markerIndices(items);
    expect(findNextMarkerIndex(markers, 3)).toBeNull();
    expect(findNextMarkerIndex(markers, 4)).toBeNull();
  });

  it("next from the sentinel -1 (no known position) is the first marker", () => {
    expect(findNextMarkerIndex(markerIndices(items), -1)).toBe(1);
  });

  it("prev marker is the last marker strictly before the current index", () => {
    const markers = markerIndices(items);
    expect(findPrevMarkerIndex(markers, 4)).toBe(3);
    expect(findPrevMarkerIndex(markers, 3)).toBe(1);
    expect(findPrevMarkerIndex(markers, 2)).toBe(1);
  });

  it("prev from a position at/before the first marker is null", () => {
    const markers = markerIndices(items);
    expect(findPrevMarkerIndex(markers, 1)).toBeNull();
    expect(findPrevMarkerIndex(markers, 0)).toBeNull();
  });
});

describe("buildDisplayRows — collapse earlier versions (Req 9.5)", () => {
  // Two earlier (stale) versions and a current one, with a marker in between.
  const timeline: TimelineItem[] = [
    makeChatExchange({ id: "v1-a", data_version: 1, is_stale: true }),
    makeChatExchange({ id: "v1-b", data_version: 1, is_stale: true }),
    makeRefreshMarker({ version: 2 }),
    makeChatExchange({ id: "v2-a", data_version: 2, is_stale: true }),
    makeRefreshMarker({ version: 3 }),
    makeChatExchange({ id: "cur", data_version: 3, is_stale: false }),
  ];

  it("returns the timeline unchanged when collapse is off", () => {
    const rows = buildDisplayRows({
      items: timeline,
      collapsed: false,
      expandedVersions: new Set(),
    });
    expect(rows).toEqual(timeline);
  });

  it("folds each earlier version into one summary row, keeping markers + current", () => {
    const rows = buildDisplayRows({
      items: timeline,
      collapsed: true,
      expandedVersions: new Set(),
    });

    // One collapsed row per earlier version (v1 count 2, v2 count 1), both
    // markers kept, and the current-version Exchange still present.
    const collapsed = rows.filter(isCollapsedVersionRow);
    expect(collapsed.map((r) => [r.version, r.count])).toEqual([
      [1, 2],
      [2, 1],
    ]);

    // Markers (v2, v3) survive the fold.
    const markerVersions = rows
      .filter((r) => !isCollapsedVersionRow(r) && r.type === "refresh_marker")
      .map((r) => (r as { version: number }).version);
    expect(markerVersions).toEqual([2, 3]);

    // The current-version Exchange is still shown expanded; no stale Exchange is.
    const exchangeIds = rows
      .filter((r) => !isCollapsedVersionRow(r) && r.type === "exchange")
      .map((r) => (r as { id: string }).id);
    expect(exchangeIds).toEqual(["cur"]);
  });

  it("places the summary row at the version's first Exchange position", () => {
    const rows = buildDisplayRows({
      items: timeline,
      collapsed: true,
      expandedVersions: new Set(),
    });
    // Row 0 is the folded v1 (was the first two rows); row 1 is the v2 marker.
    expect(isCollapsedVersionRow(rows[0]) && rows[0].version).toBe(1);
  });

  it("expanding a version restores its Exchanges while others stay folded", () => {
    const rows = buildDisplayRows({
      items: timeline,
      collapsed: true,
      expandedVersions: new Set([1]),
    });

    // v1's two Exchanges are back; v2 is still a single summary row.
    const exchangeIds = rows
      .filter((r) => !isCollapsedVersionRow(r) && r.type === "exchange")
      .map((r) => (r as { id: string }).id);
    expect(exchangeIds).toEqual(["v1-a", "v1-b", "cur"]);

    const collapsed = rows.filter(isCollapsedVersionRow);
    expect(collapsed.map((r) => r.version)).toEqual([2]);
  });
});

describe("hasEarlierVersions (Req 9.5)", () => {
  it("is true when any stale Exchange exists", () => {
    expect(
      hasEarlierVersions([
        makeChatExchange({ id: "cur", is_stale: false }),
        makeChatExchange({ id: "old", data_version: 1, is_stale: true }),
      ]),
    ).toBe(true);
  });

  it("is false with only current-version Exchanges and markers", () => {
    expect(
      hasEarlierVersions([
        makeChatExchange({ id: "cur", is_stale: false }),
        makeRefreshMarker({ version: 2 }),
      ]),
    ).toBe(false);
  });
});
