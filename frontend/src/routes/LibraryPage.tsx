/**
 * LibraryPage — the Portal's home route `/` (dataset-library task 6).
 *
 * Composes the two visually-distinct sections (Requirement 1.1):
 *   - {@link NewDatasetPanel}  — "Add new reviews" (tinted, collapsible shell
 *     hosting the dataset-ingestion tabs);
 *   - {@link TrackedDatasetsSection} — "Tracked datasets ({count})" (plain
 *     table with search / sort / Show-archived and row actions).
 *
 * Responsibilities owned here:
 * - mount {@link useRealtime} ONCE for the tab so live `dataset.status.changed`
 *   / `check.updated` frames patch the caches the sections read (task 5 hook);
 * - decide the panel's initial expand state from whether the library is empty
 *   (Requirements 1.5, 1.6);
 * - own the transient highlight set and clear each id after
 *   {@link HIGHLIGHT_MS} (Requirement 1.4), and highlight the dataset an Add
 *   navigates to;
 * - stack the two sections vertically (the layout CSS stacks on narrow
 *   viewports — Requirement 1.5).
 *
 * The page reads the list through {@link useDatasets} once (shared cache with
 * the section) only to know whether the library is empty for the panel's
 * initial state; the section drives the actual list rendering.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { useDatasets } from "../hooks/useDatasets";
import { useRealtime } from "../hooks/useRealtime";
import NewDatasetPanel from "../components/NewDatasetPanel";
import TrackedDatasetsSection from "../components/TrackedDatasetsSection";

/** How long a row stays highlighted after an Add/Refresh (design: 3 s). */
export const HIGHLIGHT_MS = 3_000;

/** Pull a dataset id out of a `/datasets/{id}` navigation path, else null. */
function datasetIdFromPath(path: string): string | null {
  const match = /^\/datasets\/([^/?#]+)/.exec(path);
  return match ? decodeURIComponent(match[1]) : null;
}

export interface LibraryPageProps {
  /** Navigate to a path (defaults to the History API). */
  onNavigate?: (path: string) => void;
  /** Disable the realtime socket (tests that don't need it set this false). */
  realtimeEnabled?: boolean;
}

/** Default navigation: push onto the History API (no router dependency). */
function defaultNavigate(path: string): void {
  if (typeof window === "undefined") return;
  window.history.pushState(null, "", path);
}

export default function LibraryPage({
  onNavigate = defaultNavigate,
  realtimeEnabled = true,
}: LibraryPageProps) {
  useRealtime({ enabled: realtimeEnabled });

  // One shared read of the list (same cache key as the section) to know whether
  // the library is empty for the panel's initial expand state.
  const listQuery = useDatasets({ archived: false, sort: "activity", q: "" });
  const isEmpty = (listQuery.data?.length ?? 0) === 0;

  const [highlightedIds, setHighlightedIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const highlight = useCallback((id: string) => {
    setHighlightedIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
    const timers = timersRef.current;
    const existing = timers.get(id);
    if (existing) clearTimeout(existing);
    timers.set(
      id,
      setTimeout(() => {
        setHighlightedIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
        timers.delete(id);
      }, HIGHLIGHT_MS),
    );
  }, []);

  // Clear any pending highlight timers on unmount.
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
  }, []);

  // Navigation triggered by an Add: highlight the affected dataset, then go to
  // its detail page (Requirements 1.4, 3.1).
  const handleAddNavigate = useCallback(
    (path: string) => {
      const id = datasetIdFromPath(path);
      if (id) highlight(id);
      onNavigate(path);
    },
    [highlight, onNavigate],
  );

  return (
    <main className="library-page" data-testid="library-page">
      <NewDatasetPanel
        defaultExpanded={isEmpty}
        collapsible={!isEmpty}
        addNavigation={{ onNavigate: handleAddNavigate }}
      />

      <TrackedDatasetsSection
        onNavigate={onNavigate}
        highlightedIds={highlightedIds}
        onHighlight={highlight}
      />
    </main>
  );
}
