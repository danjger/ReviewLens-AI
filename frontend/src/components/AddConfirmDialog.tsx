/**
 * AddConfirmDialog — the confirmation shown before Add when any selected item
 * is `limited` (Requirement 3.9; design "Frontend": "A confirmation lists the
 * `limited` items and how each item will be handled (new or refresh)").
 *
 * It lists every selected item and, for each, how it will be handled:
 * - **new** when the item has no `existing_dataset` (a fresh dataset);
 * - **refresh** when the item is already tracked (`existing_dataset` present).
 *
 * The `limited` items are called out separately with their reasons shown again
 * (Requirement 3.9). Confirming reports the set of item ids that were
 * `limited`, so the container can send `confirm_limited: true` for exactly
 * those items; cancelling closes the dialog without adding.
 *
 * This dialog only appears when the selection contains at least one `limited`
 * item; a selection of only `will_work` items is added without a prompt.
 */
import type { CheckItem } from "../api/ingest";

export interface AddConfirmDialogProps {
  /** The selected items being confirmed, in display order. */
  items: CheckItem[];
  /** Confirm: receives the item ids that are `limited` (need confirm_limited). */
  onConfirm: (limitedItemIds: string[]) => void;
  /** Cancel: close without adding. */
  onCancel: () => void;
}

/** How a selected item will be handled once added. */
function handling(item: CheckItem): "new" | "refresh" {
  return item.existing_dataset ? "refresh" : "new";
}

/** True when the item's verdict is `limited`. */
function isLimited(item: CheckItem): boolean {
  return item.verdict?.verdict === "limited";
}

export default function AddConfirmDialog({
  items,
  onConfirm,
  onCancel,
}: AddConfirmDialogProps) {
  const limitedItems = items.filter(isLimited);
  const limitedIds = limitedItems.map((item) => item.item_id);

  return (
    <div
      className="add-confirm"
      data-testid="add-confirm-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="add-confirm-title"
    >
      <h2 id="add-confirm-title" className="add-confirm__title">
        Confirm adding {items.length} URL{items.length === 1 ? "" : "s"}
      </h2>

      {/* Every selected item and how it will be handled (new or refresh). */}
      <ul className="add-confirm__items" data-testid="add-confirm-items">
        {items.map((item) => (
          <li
            key={item.item_id}
            className="add-confirm__item"
            data-testid="add-confirm-item"
            data-handling={handling(item)}
            data-limited={isLimited(item) ? "true" : "false"}
          >
            <span className="add-confirm__item-url">{item.input}</span>{" "}
            <span className="add-confirm__item-handling">
              {handling(item) === "refresh"
                ? `will refresh ${item.existing_dataset?.name ?? "the existing dataset"}`
                : "will be added as a new dataset"}
            </span>
          </li>
        ))}
      </ul>

      {/* The limited items, with their reasons shown again (Requirement 3.9). */}
      {limitedItems.length > 0 && (
        <div className="add-confirm__limited" data-testid="add-confirm-limited">
          <p className="add-confirm__limited-intro">
            {limitedItems.length === 1
              ? "One selected URL is limited. It may yield fewer or lower-quality reviews:"
              : `${limitedItems.length} selected URLs are limited. They may yield fewer or lower-quality reviews:`}
          </p>
          <ul className="add-confirm__limited-list">
            {limitedItems.map((item) => (
              <li
                key={item.item_id}
                className="add-confirm__limited-item"
                data-testid="add-confirm-limited-item"
              >
                <span className="add-confirm__limited-url">{item.input}</span>
                {item.verdict && item.verdict.reasons.length > 0 && (
                  <ul className="add-confirm__limited-reasons">
                    {item.verdict.reasons.map((reason, index) => (
                      <li key={index} data-testid="add-confirm-limited-reason">
                        {reason}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="add-confirm__actions">
        <button
          type="button"
          className="add-confirm__cancel"
          data-testid="add-confirm-cancel"
          onClick={onCancel}
        >
          Cancel
        </button>
        <button
          type="button"
          className="add-confirm__confirm"
          data-testid="add-confirm-confirm"
          onClick={() => onConfirm(limitedIds)}
        >
          Confirm and add
        </button>
      </div>
    </div>
  );
}
