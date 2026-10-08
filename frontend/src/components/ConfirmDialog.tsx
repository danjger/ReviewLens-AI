/**
 * ConfirmDialog — a small, accessible confirm/cancel modal used for the
 * destructive-ish actions on a dataset row (dataset-library task 6.4).
 *
 * Used for Archive (Requirement 5.1: "chooses Archive and confirms"). It is a
 * `role="dialog"` with an accessible name from its title, a message body, and
 * two buttons; Escape and the Cancel button both cancel.
 */
import { useCallback, useEffect, useId } from "react";

export interface ConfirmDialogProps {
  title: string;
  message: string;
  /** Confirm button label (default "Confirm"). */
  confirmLabel?: string;
  /** Cancel button label (default "Cancel"). */
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function ConfirmDialog({
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
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
      className="confirm-dialog"
      data-testid="confirm-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
    >
      <div className="confirm-dialog__panel">
        <h2 id={titleId} className="confirm-dialog__title">
          {title}
        </h2>
        <p className="confirm-dialog__message">{message}</p>
        <div className="confirm-dialog__actions">
          <button
            type="button"
            data-testid="confirm-dialog-cancel"
            onClick={onCancel}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            data-testid="confirm-dialog-confirm"
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
