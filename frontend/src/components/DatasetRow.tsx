/**
 * DatasetRow — one row of the Tracked Datasets table (dataset-library tasks
 * 6.3, 6.4, 6.5).
 *
 * Shows (design "Frontend" / Requirement 2.1):
 *   name · main URL, the {@link StatusBadge} with the live progress message,
 *   review count, data version, and a relative last-refreshed time, plus the
 *   {@link RowActionsMenu}.
 *
 * Navigation (Requirement 3.1): clicking the row (or Open) goes to
 * `/datasets/{id}`. The whole row is clickable; the actions menu stops
 * propagation so using it never navigates.
 *
 * Refresh (Requirements 4.3, 4.4): Refresh is disabled while the dataset is
 * `requested`/`processing` or a refresh Check is running. While a refresh Check
 * runs the badge shows "Checking page…". When that Check comes back `limited`
 * (`awaiting_confirmation`), the row shows a "Needs confirmation" button that
 * opens {@link RefreshConfirmDialog} with the verdict reasons; confirming calls
 * the confirm endpoint. A `ready_refresh_failed` state shows the failure reason.
 *
 * Highlight (Requirement 1.4): when `highlighted` is set the row gets a
 * transient highlight class and scrolls itself into view.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";

import type { Dataset } from "../api/datasets";
import { getCheck, type CheckItem } from "../api/ingest";
import { checkRefetchInterval } from "../hooks/realtimePolling";
import { checkQueryKey } from "../hooks/useCheck";
import ConfirmDialog from "./ConfirmDialog";
import RefreshConfirmDialog from "./RefreshConfirmDialog";
import DatasetThumbnail from "./DatasetThumbnail";
import RowActionsMenu from "./RowActionsMenu";
import StatusBadge from "./StatusBadge";
import { relativeTime } from "./relativeTime";

export interface DatasetRowProps {
  dataset: Dataset;
  /** Highlight + scroll this row (set briefly after an Add/Refresh). */
  highlighted?: boolean;
  /** Navigate to a path (defaults to the History API in the parent). */
  onNavigate: (path: string) => void;
  onRefresh: (id: string) => void;
  onConfirmRefresh: (id: string, checkId: string) => void;
  onArchive: (id: string) => void;
  onRestore: (id: string) => void;
}

/** Statuses that mean a refresh/processing run is in flight (disable Refresh). */
const IN_FLIGHT = new Set(["requested", "processing"]);

/** URL datasets can be refreshed from the row; uploads need a replacement file.
 *  Prefer the authoritative `source_type` from the API; fall back to sniffing
 *  the URL only if an older cached row lacks it. */
function isUrlDataset(dataset: Dataset): boolean {
  if (dataset.source_type) return dataset.source_type === "url";
  return /^https?:\/\//i.test(dataset.main_url);
}

export default function DatasetRow({
  dataset,
  highlighted = false,
  onNavigate,
  onRefresh,
  onConfirmRefresh,
  onArchive,
  onRestore,
}: DatasetRowProps) {
  const rowRef = useRef<HTMLTableRowElement>(null);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const refreshChecking = dataset.refresh_check_id != null;

  // While a refresh Check is running/awaiting confirmation, watch it so the row
  // can offer "Needs confirmation" with the verdict reasons (Requirement 4.3).
  const checkQuery = useQuery({
    queryKey: checkQueryKey(dataset.refresh_check_id),
    queryFn: () => getCheck(dataset.refresh_check_id as string),
    enabled: dataset.refresh_check_id != null,
    refetchInterval: (query) => checkRefetchInterval(query.state.data),
  });

  // The single item of a refresh Check (the original URL being re-checked).
  const refreshItem: CheckItem | undefined = checkQuery.data?.items[0];
  const needsConfirmation =
    refreshItem?.state === "awaiting_confirmation" &&
    refreshItem.verdict?.verdict === "limited";
  const limitedReasons = refreshItem?.verdict?.reasons ?? [];

  // Scroll a freshly-highlighted row into view (Requirement 1.4).
  useEffect(() => {
    const node = rowRef.current;
    // Guard: scrollIntoView is unimplemented under jsdom (and may be absent in
    // other non-browser hosts), so only call it when available.
    if (highlighted && node && typeof node.scrollIntoView === "function") {
      node.scrollIntoView({ block: "nearest" });
    }
  }, [highlighted]);

  const refreshDisabled =
    IN_FLIGHT.has(dataset.status) || refreshChecking || dataset.archived_at != null;

  const detailPath = `/datasets/${encodeURIComponent(dataset.id)}`;
  const open = () => onNavigate(detailPath);

  return (
    <tr
      ref={rowRef}
      className={`dataset-row${highlighted ? " dataset-row--highlight" : ""}`}
      data-testid="dataset-row"
      data-dataset-id={dataset.id}
      data-highlighted={highlighted ? "true" : "false"}
      onClick={open}
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === "Enter") open();
      }}
    >
      <td className="dataset-row__name" data-testid="row-name">
        <DatasetThumbnail
          sourceType={dataset.source_type}
          thumbnailUrl={dataset.thumbnail_url}
          name={dataset.name}
        />
        <span className="dataset-row__name-text">
          <span className="dataset-row__title">{dataset.name}</span>
          <span className="dataset-row__url" data-testid="row-url">
            {dataset.main_url}
          </span>
        </span>
      </td>

      <td className="dataset-row__status">
        <StatusBadge
          displayState={dataset.display_state}
          lastMessage={dataset.last_message}
          refreshChecking={refreshChecking}
        />
        {needsConfirmation && (
          <button
            type="button"
            className="dataset-row__needs-confirm"
            data-testid="needs-confirmation-button"
            onClick={(event) => {
              event.stopPropagation();
              setConfirmOpen(true);
            }}
          >
            Needs confirmation
          </button>
        )}
        {dataset.display_state === "ready_refresh_failed" && dataset.last_message && (
          <span
            className="dataset-row__refresh-failed"
            data-testid="refresh-failed-reason"
          >
            {dataset.last_message}
          </span>
        )}
      </td>

      <td className="dataset-row__reviews" data-testid="row-review-count">
        {dataset.review_count ?? "—"}
      </td>

      <td className="dataset-row__version" data-testid="row-version">
        {dataset.data_version != null ? `v${dataset.data_version}` : "—"}
      </td>

      <td className="dataset-row__refreshed" data-testid="row-refreshed">
        {relativeTime(dataset.last_refreshed_at)}
      </td>

      <td
        className="dataset-row__actions"
        onClick={(event) => event.stopPropagation()}
      >
        <RowActionsMenu
          canRefresh={isUrlDataset(dataset)}
          refreshDisabled={refreshDisabled}
          archived={dataset.archived_at != null}
          onOpen={open}
          onRefresh={() => onRefresh(dataset.id)}
          onArchive={() => setArchiveOpen(true)}
          onRestore={() => onRestore(dataset.id)}
        />
      </td>

      {/* Dialogs render through a portal to document.body so they are valid
          modal overlays rather than table-cell content. */}
      {archiveOpen &&
        createPortal(
          <ConfirmDialog
            title="Archive dataset"
            message={`Archive "${dataset.name}"? Its data is kept and you can restore it from "Show archived".`}
            confirmLabel="Archive"
            onConfirm={() => {
              setArchiveOpen(false);
              onArchive(dataset.id);
            }}
            onCancel={() => setArchiveOpen(false)}
          />,
          document.body,
        )}

      {confirmOpen &&
        dataset.refresh_check_id &&
        createPortal(
          <RefreshConfirmDialog
            datasetName={dataset.name}
            reasons={limitedReasons}
            onConfirm={() => {
              setConfirmOpen(false);
              onConfirmRefresh(dataset.id, dataset.refresh_check_id as string);
            }}
            onCancel={() => setConfirmOpen(false)}
          />,
          document.body,
        )}
    </tr>
  );
}
