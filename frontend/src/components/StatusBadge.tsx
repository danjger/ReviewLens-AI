/**
 * StatusBadge — the Tracked Datasets row status badge (dataset-library task 6.3,
 * Requirements 2.5, 6.3).
 *
 * It renders the server-derived `display_state` exactly per the design's badge
 * mapping, as TEXT (not color alone, for accessibility):
 *
 *   processing            → "Processing" + the latest live message (spinner)
 *   ready                 → "Ready"
 *   ready_refreshing      → "Ready · refreshing" (spinner)
 *   ready_refresh_failed  → "Ready · last refresh failed" (warning; the reason
 *                           is exposed via a tooltip/title from `last_message`)
 *   failed                → "Failed" (error; last event message as the title)
 *
 * While a refresh Check is running or awaiting confirmation (`refresh_check_id`
 * set), the badge instead shows "Checking page…" so the analyst sees the
 * re-check progress on the row (design "Frontend").
 *
 * Counts and dates are deliberately NOT part of the badge text (testing.md:
 * never assert on text with counts/dates); the live message is surfaced via a
 * dedicated testid so tests can target it without matching counts.
 */
import type { DisplayState } from "../api/datasets";

export interface StatusBadgeProps {
  displayState: DisplayState;
  /** The latest live progress / event message, shown for in-flight states. */
  lastMessage?: string | null;
  /** True while a refresh Check is running or awaiting confirmation. */
  refreshChecking?: boolean;
}

/** Static label + semantic tone for each display state. */
const LABELS: Record<DisplayState, { label: string; tone: string }> = {
  processing: { label: "Processing", tone: "processing" },
  ready: { label: "Ready", tone: "ready" },
  ready_refreshing: { label: "Ready · refreshing", tone: "refreshing" },
  ready_refresh_failed: {
    label: "Ready · last refresh failed",
    tone: "refresh-failed",
  },
  failed: { label: "Failed", tone: "failed" },
};

/** Display states that show an in-progress spinner. */
const SPINNING: ReadonlySet<DisplayState> = new Set<DisplayState>([
  "processing",
  "ready_refreshing",
]);

/** Display states whose `last_message` is surfaced as a tooltip (the reason). */
const TOOLTIP_STATES: ReadonlySet<DisplayState> = new Set<DisplayState>([
  "ready_refresh_failed",
  "failed",
]);

export default function StatusBadge({
  displayState,
  lastMessage,
  refreshChecking = false,
}: StatusBadgeProps) {
  // A running refresh Check takes visual priority: the row shows the re-check.
  if (refreshChecking) {
    return (
      <span
        className="status-badge status-badge--checking"
        data-testid="status-badge"
        data-state="checking"
        role="status"
      >
        <span className="status-badge__spinner" aria-hidden="true" />
        Checking page…
      </span>
    );
  }

  const { label, tone } = LABELS[displayState];
  const spinning = SPINNING.has(displayState);
  const title =
    TOOLTIP_STATES.has(displayState) && lastMessage ? lastMessage : undefined;

  return (
    <span
      className={`status-badge status-badge--${tone}`}
      data-testid="status-badge"
      data-state={displayState}
      title={title}
      {...(spinning ? { role: "status" } : {})}
    >
      {spinning && <span className="status-badge__spinner" aria-hidden="true" />}
      <span className="status-badge__label">{label}</span>
      {displayState === "processing" && lastMessage && (
        <span className="status-badge__message" data-testid="status-message">
          {" — "}
          {lastMessage}
        </span>
      )}
    </span>
  );
}
