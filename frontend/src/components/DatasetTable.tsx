/**
 * DatasetTable — the table of Tracked Datasets rows (dataset-library task 6.3).
 *
 * Pure presentation around {@link DatasetRow}: it renders the column headers
 * and maps the datasets to rows, forwarding row-level callbacks and the
 * highlight set. Loading / empty / error states are handled by the hosting
 * {@link TrackedDatasetsSection}; this component assumes it has rows to show.
 */
import type { Dataset } from "../api/datasets";
import DatasetRow from "./DatasetRow";

export interface DatasetTableProps {
  datasets: Dataset[];
  /** Ids currently highlighted (after an Add/Refresh — Requirement 1.4). */
  highlightedIds: ReadonlySet<string>;
  onNavigate: (path: string) => void;
  onRefresh: (id: string) => void;
  onConfirmRefresh: (id: string, checkId: string) => void;
  onArchive: (id: string) => void;
  onRestore: (id: string) => void;
}

export default function DatasetTable({
  datasets,
  highlightedIds,
  onNavigate,
  onRefresh,
  onConfirmRefresh,
  onArchive,
  onRestore,
}: DatasetTableProps) {
  return (
    <table className="dataset-table" data-testid="dataset-table">
      <thead>
        <tr>
          <th scope="col">Dataset</th>
          <th scope="col">Status</th>
          <th scope="col">Reviews</th>
          <th scope="col">Version</th>
          <th scope="col">Last refreshed</th>
          <th scope="col">
            <span className="visually-hidden">Actions</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {datasets.map((dataset) => (
          <DatasetRow
            key={dataset.id}
            dataset={dataset}
            highlighted={highlightedIds.has(dataset.id)}
            onNavigate={onNavigate}
            onRefresh={onRefresh}
            onConfirmRefresh={onConfirmRefresh}
            onArchive={onArchive}
            onRestore={onRestore}
          />
        ))}
      </tbody>
    </table>
  );
}
