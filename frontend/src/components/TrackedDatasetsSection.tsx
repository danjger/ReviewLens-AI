/**
 * TrackedDatasetsSection — the "Tracked datasets" half of the Library
 * (dataset-library tasks 6.3, 6.4, 6.5, 6.6).
 *
 * Owns the list controls (search text, sort order, "Show archived" toggle),
 * reads the list through {@link useDatasets}, and renders {@link DatasetTable}
 * with loading / empty / error-with-retry states (Requirements 2.4, 1.6).
 *
 * It wires the row actions to the dataset mutations ({@link useArchiveDataset},
 * {@link useRestoreDataset}, {@link useRefreshDataset},
 * {@link useConfirmRefresh}) and, on a Refresh, highlights the affected row
 * (Requirement 1.4). The section header shows the count: "Tracked datasets
 * ({count})" (Requirement 1.1).
 *
 * It contains NO URL input (Requirement 1.3): its only inputs are the search
 * box and the sort/archived controls, which act on existing datasets.
 */
import { useCallback, useId, useState } from "react";

import type { DatasetSort } from "../api/datasets";
import {
  useArchiveDataset,
  useConfirmRefresh,
  useDatasets,
  useRefreshDataset,
  useRestoreDataset,
  type DatasetListControls,
} from "../hooks/useDatasets";
import DatasetTable from "./DatasetTable";

export interface TrackedDatasetsSectionProps {
  /** Navigate to a path (History API by default from the page). */
  onNavigate: (path: string) => void;
  /** Ids to highlight (Add highlights from the page; Refresh adds its own). */
  highlightedIds: ReadonlySet<string>;
  /** Report an id that should be highlighted (e.g. after a Refresh). */
  onHighlight: (id: string) => void;
}

const SORT_OPTIONS: Array<{ value: DatasetSort; label: string }> = [
  { value: "activity", label: "Last activity" },
  { value: "name", label: "Name" },
  { value: "status", label: "Status" },
  { value: "refreshed", label: "Last refreshed" },
];

export default function TrackedDatasetsSection({
  onNavigate,
  highlightedIds,
  onHighlight,
}: TrackedDatasetsSectionProps) {
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<DatasetSort>("activity");
  const [archived, setArchived] = useState(false);
  const searchId = useId();
  const sortId = useId();

  const controls: DatasetListControls = { archived, sort, q };
  const query = useDatasets(controls);

  const archiveMutation = useArchiveDataset();
  const restoreMutation = useRestoreDataset();
  const refreshMutation = useRefreshDataset();
  const confirmMutation = useConfirmRefresh();

  const handleRefresh = useCallback(
    (id: string) => {
      refreshMutation.mutate(id, { onSuccess: () => onHighlight(id) });
    },
    [refreshMutation, onHighlight],
  );

  const handleConfirmRefresh = useCallback(
    (id: string, checkId: string) => {
      confirmMutation.mutate({ id, checkId }, { onSuccess: () => onHighlight(id) });
    },
    [confirmMutation, onHighlight],
  );

  const handleArchive = useCallback(
    (id: string) => archiveMutation.mutate(id),
    [archiveMutation],
  );

  const handleRestore = useCallback(
    (id: string) => restoreMutation.mutate(id),
    [restoreMutation],
  );

  const datasets = query.data ?? [];
  const count = datasets.length;

  return (
    <section
      className="tracked-section"
      data-testid="tracked-section"
      aria-labelledby="tracked-heading"
    >
      <header className="tracked-section__header">
        <h2 id="tracked-heading" className="tracked-section__title">
          Tracked datasets{" "}
          <span data-testid="tracked-count">({count})</span>
        </h2>

        <div className="tracked-section__controls">
          <div className="tracked-section__search">
            <label htmlFor={searchId} className="visually-hidden">
              Search datasets
            </label>
            <input
              id={searchId}
              type="search"
              data-testid="tracked-search"
              placeholder="Search name or URL…"
              value={q}
              onChange={(event) => setQ(event.target.value)}
            />
          </div>

          <div className="tracked-section__sort">
            <label htmlFor={sortId}>Sort</label>
            <select
              id={sortId}
              data-testid="tracked-sort"
              value={sort}
              onChange={(event) => setSort(event.target.value as DatasetSort)}
            >
              {SORT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <label className="tracked-section__archived">
            <input
              type="checkbox"
              data-testid="tracked-show-archived"
              checked={archived}
              onChange={(event) => setArchived(event.target.checked)}
            />
            Show archived
          </label>
        </div>
      </header>

      {query.isLoading ? (
        <div
          className="tracked-section__loading"
          data-testid="tracked-loading"
          role="status"
        >
          Loading datasets…
        </div>
      ) : query.isError ? (
        <div
          className="tracked-section__error"
          data-testid="tracked-error"
          role="alert"
        >
          <p>Couldn’t load your datasets.</p>
          <button
            type="button"
            data-testid="tracked-retry"
            onClick={() => void query.refetch()}
          >
            Retry
          </button>
        </div>
      ) : count === 0 ? (
        <div className="tracked-section__empty" data-testid="tracked-empty">
          {archived ? (
            <p>No archived datasets.</p>
          ) : q.trim().length > 0 ? (
            <p>No datasets match “{q.trim()}”.</p>
          ) : (
            <p>
              No datasets yet. Add your first one in the{" "}
              <a href="#add-new-reviews" data-testid="empty-add-link">
                Add new reviews
              </a>{" "}
              panel above.
            </p>
          )}
        </div>
      ) : (
        <DatasetTable
          datasets={datasets}
          highlightedIds={highlightedIds}
          onNavigate={onNavigate}
          onRefresh={handleRefresh}
          onConfirmRefresh={handleConfirmRefresh}
          onArchive={handleArchive}
          onRestore={handleRestore}
        />
      )}
    </section>
  );
}
