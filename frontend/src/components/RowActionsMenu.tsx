/**
 * RowActionsMenu — the "⋯" actions menu on a Tracked Datasets row
 * (dataset-library task 6.4, Requirements 4.3, 4.4, 5.1, 5.3).
 *
 * Presentational: it renders the available actions and reports clicks; the
 * hosting {@link DatasetRow} owns the mutations, dialogs, and disabled logic.
 *
 * Actions:
 *   - Open      — always available (navigates to the detail page).
 *   - Refresh   — URL datasets only; disabled while processing or while a
 *                 refresh Check is running (Requirement 4.4).
 *   - Archive   — shown for a non-archived dataset.
 *   - Restore   — shown for an archived dataset (Requirement 5.3).
 *
 * The menu is a simple disclosure: a toggle button reveals a list of actions.
 */
import { useCallback, useEffect, useRef, useState } from "react";

export interface RowActionsMenuProps {
  /** Whether a Refresh action should be offered (URL datasets only). */
  canRefresh: boolean;
  /** Disable Refresh (processing, or a refresh Check already running). */
  refreshDisabled: boolean;
  /** True when the dataset is archived (show Restore instead of Archive). */
  archived: boolean;
  onOpen: () => void;
  onRefresh: () => void;
  onArchive: () => void;
  onRestore: () => void;
}

export default function RowActionsMenu({
  canRefresh,
  refreshDisabled,
  archived,
  onOpen,
  onRefresh,
  onArchive,
  onRestore,
}: RowActionsMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => setOpen(false), []);

  // Close on an outside click or Escape so the menu behaves like a popover.
  useEffect(() => {
    if (!open) return;
    function onDocClick(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        close();
      }
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") close();
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);

  const run = useCallback(
    (action: () => void) => () => {
      close();
      action();
    },
    [close],
  );

  return (
    <div className="row-actions" data-testid="row-actions" ref={rootRef}>
      <button
        type="button"
        className="row-actions__toggle"
        data-testid="row-actions-toggle"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Dataset actions"
        onClick={() => setOpen((value) => !value)}
      >
        ⋯
      </button>

      {open && (
        <ul className="row-actions__menu" data-testid="row-actions-menu" role="menu">
          <li role="none">
            <button
              type="button"
              role="menuitem"
              data-testid="action-open"
              onClick={run(onOpen)}
            >
              Open
            </button>
          </li>

          {canRefresh && (
            <li role="none">
              <button
                type="button"
                role="menuitem"
                data-testid="action-refresh"
                disabled={refreshDisabled}
                onClick={run(onRefresh)}
              >
                Refresh
              </button>
            </li>
          )}

          {archived ? (
            <li role="none">
              <button
                type="button"
                role="menuitem"
                data-testid="action-restore"
                onClick={run(onRestore)}
              >
                Restore
              </button>
            </li>
          ) : (
            <li role="none">
              <button
                type="button"
                role="menuitem"
                data-testid="action-archive"
                onClick={run(onArchive)}
              >
                Archive
              </button>
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
