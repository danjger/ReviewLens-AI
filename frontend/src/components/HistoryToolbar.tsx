/**
 * HistoryToolbar — the toolbar above the Q&A history (guardrailed-chat task
 * 6.7, Requirement 9.5).
 *
 * Two controls, both operating on {@link HistoryPane}'s flattened timeline:
 *
 * - **Jump to refresh**: previous/next buttons that scroll the pane to the
 *   previous or next {@link RefreshMarker} relative to the row currently in
 *   view. The toolbar is presentational — it calls `onJumpPrev`/`onJumpNext`,
 *   and the pane does the actual `scrollToIndex` against the virtualizer (the
 *   marker-index math lives in `historyCollapse.ts`). When there are no markers
 *   to jump to, both buttons are disabled.
 * - **Collapse earlier versions**: a toggle (a real `<button>` with
 *   `aria-expanded`) that folds every earlier version's Exchanges into one
 *   summary row per version, keeping markers and current-version Exchanges
 *   (Requirement 9.5). It is disabled when there are no earlier versions to
 *   fold. `collapsed` reflects the current state; `onToggleCollapse` flips it.
 *
 * The pane owns the state (the collapse flag, the per-version expansion, and
 * the virtualizer) and passes callbacks + the small booleans this toolbar needs
 * (`hasMarkers`, `canCollapse`), so the toolbar stays a thin, testable shell.
 *
 * **Accessibility.** Every control is a real `<button>` with an explicit
 * `aria-label` (the visible glyphs/short copy aren't descriptive on their own).
 * The collapse toggle carries `aria-pressed`/`aria-expanded` so assistive tech
 * announces its on/off state. Buttons that have nothing to act on are
 * `disabled` rather than hidden, so the toolbar's shape stays stable.
 *
 * **Testing hooks (testing.md: data-testid / data-* only).** The toolbar and
 * each control carry a `data-testid`; the collapse toggle exposes its state via
 * `data-collapsed`. Nothing here renders counts or dates, so there is no
 * formatted-number assertion surface.
 */

export interface HistoryToolbarProps {
  /** Scroll to the previous refresh marker (relative to the current view). */
  onJumpPrev: () => void;
  /** Scroll to the next refresh marker (relative to the current view). */
  onJumpNext: () => void;
  /**
   * Whether the timeline has any refresh markers to jump between. When false
   * both jump buttons are disabled (nothing to jump to).
   */
  hasMarkers: boolean;
  /** Whether "collapse earlier versions" is currently on. */
  collapsed: boolean;
  /** Flip the collapse toggle. */
  onToggleCollapse: () => void;
  /**
   * Whether there are any earlier-version Exchanges to fold. When false the
   * collapse toggle is disabled (nothing to collapse).
   */
  canCollapse: boolean;
}

export default function HistoryToolbar({
  onJumpPrev,
  onJumpNext,
  hasMarkers,
  collapsed,
  onToggleCollapse,
  canCollapse,
}: HistoryToolbarProps) {
  return (
    <div
      className="history-toolbar"
      data-testid="history-toolbar"
      role="toolbar"
      aria-label="History controls"
    >
      <div className="history-toolbar__jump" role="group" aria-label="Jump to refresh">
        <button
          type="button"
          className="history-toolbar__jump-prev"
          data-testid="history-jump-prev"
          onClick={onJumpPrev}
          disabled={!hasMarkers}
          aria-label="Jump to previous refresh"
        >
          ↑ Refresh
        </button>
        <button
          type="button"
          className="history-toolbar__jump-next"
          data-testid="history-jump-next"
          onClick={onJumpNext}
          disabled={!hasMarkers}
          aria-label="Jump to next refresh"
        >
          ↓ Refresh
        </button>
      </div>

      <button
        type="button"
        className="history-toolbar__collapse"
        data-testid="history-collapse-toggle"
        data-collapsed={collapsed ? "true" : "false"}
        onClick={onToggleCollapse}
        disabled={!canCollapse}
        aria-pressed={collapsed}
        aria-expanded={!collapsed}
        aria-label="Collapse earlier versions"
      >
        {collapsed ? "Show earlier versions" : "Collapse earlier versions"}
      </button>
    </div>
  );
}
