/**
 * RefreshMarker — a full-width divider in the Q&A timeline announcing a data
 * refresh (guardrailed-chat task 6.6, Requirements 9.1, 9.2, 9.3).
 *
 * One marker is emitted per data version after the first (design "Refresh
 * markers"). It has three states, driven by {@link RefreshMarker.state}:
 *
 * - **completed** (Requirement 9.1): a refresh icon and
 *   "Data refreshed {local datetime} · v{n} · {count} reviews (was {prev})",
 *   plus how it was started — the {@link TRIGGER_WORDING} map turns the
 *   backend's `trigger` into human copy ("Refreshed manually" /
 *   "Refreshed because the URL was submitted again" / "Replacement file
 *   uploaded").
 * - **pending** (Requirement 9.2): a spinner and "Refreshing data…", shown at
 *   the end of the history while a refresh is `requested`/`processing`.
 * - **failed** (Requirement 9.3): "Refresh failed — answers still reflect
 *   v{n-1}", so the analyst knows the current data version did not change.
 *
 * **Local datetime (Requirement 9.1).** The completed marker shows the refresh
 * time in the viewer's local time via `toLocaleString`, consistent with
 * {@link DatasetHeader}/{@link ProcessingTimeline}. The machine-readable ISO
 * instant is on a `<time dateTime>` element so tests assert on the attribute,
 * never the formatted text.
 *
 * **Accessibility.** The marker is `role="separator"` with an accessible label
 * summarising the state and version (so a screen reader hears the boundary
 * rather than a loose run of text). The pending spinner carries
 * `role="status"` + an accessible label so assistive tech announces that a
 * refresh is in progress.
 *
 * **Testing (testing.md).** The visible text contains the counts and dates for
 * users, but every value a test needs is also exposed as a `data-*` attribute
 * (`data-version`, `data-state`, `data-trigger`, `data-review-count`,
 * `data-previous-review-count`), so tests assert on structure/state without
 * matching formatted count/date strings.
 */
import type { RefreshMarker as RefreshMarkerItem } from "../api/chat";

export interface RefreshMarkerProps {
  /** The refresh-marker timeline row to render. */
  marker: RefreshMarkerItem;
}

/**
 * Map the backend `trigger` to the user-facing "how it was started" wording
 * (design "Frontend (ChatPanel)" / Requirement 9.1). An unknown trigger falls
 * back to the neutral "Refreshed" so a new backend trigger never renders blank.
 */
const TRIGGER_WORDING: Record<string, string> = {
  manual_refresh: "Refreshed manually",
  duplicate_submission: "Refreshed because the URL was submitted again",
  upload_replace: "Replacement file uploaded",
};

/** Resolve a trigger code to its wording, with a safe fallback. */
function triggerWording(trigger: string): string {
  return TRIGGER_WORDING[trigger] ?? "Refreshed";
}

/** Format an ISO timestamp as a readable local date-time, or `null`. */
function formatDateTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime();
  if (Number.isNaN(ms)) return null;
  return new Date(ms).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Build the `role="separator"` accessible label for the current state. */
function markerLabel(marker: RefreshMarkerItem): string {
  switch (marker.state) {
    case "pending":
      return "Refreshing data";
    case "failed":
      return `Refresh to version ${marker.version} failed; answers still reflect version ${marker.version - 1}`;
    default:
      return `Data refreshed to version ${marker.version}`;
  }
}

export default function RefreshMarker({ marker }: RefreshMarkerProps) {
  const completedText = formatDateTime(marker.completed_at);

  return (
    <div
      className={`refresh-marker refresh-marker--${marker.state}`}
      data-testid="refresh-marker"
      data-version={marker.version}
      data-state={marker.state}
      data-trigger={marker.trigger}
      data-review-count={marker.review_count ?? undefined}
      data-previous-review-count={marker.previous_review_count ?? undefined}
      role="separator"
      aria-label={markerLabel(marker)}
    >
      <span className="refresh-marker__icon" aria-hidden="true">
        ⟳
      </span>

      {marker.state === "pending" && (
        <span
          className="refresh-marker__pending"
          data-testid="refresh-marker-pending"
          role="status"
        >
          <span
            className="refresh-marker__spinner"
            data-testid="refresh-marker-spinner"
            role="img"
            aria-label="Refreshing data"
          />
          <span className="refresh-marker__text">Refreshing data…</span>
        </span>
      )}

      {marker.state === "failed" && (
        <span className="refresh-marker__failed" data-testid="refresh-marker-failed">
          Refresh failed — answers still reflect v{marker.version - 1}
        </span>
      )}

      {marker.state === "completed" && (
        <span className="refresh-marker__completed" data-testid="refresh-marker-completed">
          <span className="refresh-marker__summary">
            Data refreshed{" "}
            {completedText != null && marker.completed_at != null ? (
              <time dateTime={marker.completed_at} data-testid="refresh-marker-time">
                {completedText}
              </time>
            ) : null}{" "}
            · v{marker.version} · {marker.review_count ?? "—"} reviews
            {marker.previous_review_count != null && (
              <> (was {marker.previous_review_count})</>
            )}
          </span>
          <span className="refresh-marker__trigger" data-testid="refresh-marker-trigger">
            {triggerWording(marker.trigger)}
          </span>
        </span>
      )}
    </div>
  );
}
