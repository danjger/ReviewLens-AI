/**
 * RefreshConfirmDialog — the "Needs confirmation" dialog for a `limited` refresh
 * verdict (dataset-library task 6.4, Requirement 4.3).
 *
 * When a refresh re-check returns `limited` on a dataset that was previously
 * `will_work`, the row shows a "Needs confirmation" button. Opening it shows
 * this dialog, which lists the verdict reasons so the analyst can decide whether
 * to proceed. Confirming calls the `refresh/confirm` endpoint (the caller wires
 * that through `useConfirmRefresh`); cancelling leaves the Check waiting.
 */
import { useCallback, useEffect, useId } from "react";

export interface RefreshConfirmDialogProps {
  /** The dataset name, for the dialog heading. */
  datasetName: string;
  /** The `limited` verdict reasons to show. */
  reasons: string[];
  onConfirm: () => void;
  onCancel: () => void;
}

export default function RefreshConfirmDialog({
  datasetName,
  reasons,
  onConfirm,
  onCancel,
}: RefreshConfirmDialogProps) {
  const titleId = useId();

  const handleKeyDown = useCallback(
    (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    },
    [onCancel],
  );

  useEffect(() => {
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  return (
    <div
      className="refresh-confirm-dialog"
      data-testid="refresh-confirm-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
    >
      <div className="refresh-confirm-dialog__panel">
        <h2 id={titleId} className="refresh-confirm-dialog__title">
          Needs confirmation
        </h2>
        <p className="refresh-confirm-dialog__intro">
          The re-check of <strong>{datasetName}</strong> came back limited.
          Review the reasons before refreshing:
        </p>
        {reasons.length > 0 && (
          <ul
            className="refresh-confirm-dialog__reasons"
            data-testid="refresh-confirm-reasons"
          >
            {reasons.map((reason, index) => (
              <li key={index} data-testid="refresh-confirm-reason">
                {reason}
              </li>
            ))}
          </ul>
        )}
        <div className="refresh-confirm-dialog__actions">
          <button
            type="button"
            data-testid="refresh-confirm-cancel"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            type="button"
            data-testid="refresh-confirm-confirm"
            onClick={onConfirm}
          >
            Refresh anyway
          </button>
        </div>
      </div>
    </div>
  );
}
