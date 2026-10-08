/**
 * detailPageState — the pure derivation of the detail page's live display state
 * (ingestion-summary task 6.3, Requirements 6.2 and 6.4).
 *
 * The detail page has three live states that change what the summary layout
 * shows while a dataset is being processed or has failed. This module derives
 * which one applies from the dataset's `status`, `active_version`, and
 * `display_state` (the server-derived badge state shared with the Library),
 * plus the latest progress `last_message`. Keeping the derivation pure (no DOM,
 * no hooks) lets {@link DatasetDetailPage} and the component tests reason about
 * it directly, mirroring how {@link deriveReadiness} is structured.
 *
 * **The states (design "Live behavior").**
 *
 * - **First-version processing** (Requirement 6.2, first branch) — `status` is
 *   `requested` or `processing` AND there is **no active version yet**. The
 *   metric/snapshot/reviews slots show skeleton placeholders, the latest
 *   `last_message` is shown as a progress line, and the **chat input is
 *   disabled** (there is no data to ground answers in). This maps to the
 *   `display_state` of `processing` with `active_version == null`.
 *
 * - **Refresh in progress** (Requirement 6.2, second branch) — there **is** an
 *   active version while a newer version is processing (`display_state ===
 *   "ready_refreshing"`). The active version's data and the chat **stay
 *   available**; the "refreshing" note is owned by {@link ReadinessBanner}
 *   (task 4.3), so this module only records that the page is in the normal,
 *   fully-usable state.
 *
 * - **Failed** (Requirement 6.4) — `display_state` is `failed` (no active
 *   version) or `ready_refresh_failed` (an earlier version is still active).
 *   The page shows the failure message and a Refresh action to retry. When an
 *   earlier version is still active (`ready_refresh_failed`), the data stays
 *   available and the failure note also says the analysis is "still showing
 *   v{active}". When there is no active version (`failed`), the slots fall back
 *   to the skeleton placeholders (there is nothing to show).
 *
 * **Why both `status` and `display_state`.** `display_state` already folds the
 * common cases (`processing` / `ready_refreshing` / `ready_refresh_failed` /
 * `failed`), but Requirement 6.2's first-version rule is specifically about the
 * raw lifecycle `status` being `requested`/`processing` with no active version.
 * We key the first-version-processing decision off `status` + `active_version`
 * (so it is robust even if `display_state` lags), and the refresh/failed notes
 * off `display_state` (consistent with {@link ReadinessBanner}).
 */
import type { DatasetDetail } from "../api/datasets";

/** The lifecycle statuses that mean "work is in flight" (Requirement 6.2). */
const IN_PROGRESS_STATUSES: ReadonlySet<string> = new Set([
  "requested",
  "processing",
]);

/** The distinct live display states the detail page renders. */
export type DetailPhase =
  /** First version is processing and no version is active yet. */
  | "first_version_processing"
  /** A version is active (optionally while a refresh runs): fully usable. */
  | "active"
  /** The last run failed and no version is active. */
  | "failed_no_version"
  /** The last refresh failed but an earlier version is still active. */
  | "failed_showing_previous";

/** The fully-derived live state the detail page renders. */
export interface DetailPageState {
  /** Which of the four live phases applies. */
  phase: DetailPhase;
  /**
   * Whether the metric/snapshot/reviews slots should show skeleton
   * placeholders instead of their (absent) data. True only when there is no
   * active version to show (first-version processing, or failed with no
   * version).
   */
  showSkeletons: boolean;
  /**
   * Whether the chat input must be disabled. True exactly when there is no
   * active version to ground answers in (Requirement 6.2: "IF there is no
   * active version yet … the chat input SHALL be disabled"). An active version
   * — even while a refresh runs or after a refresh failed — keeps chat usable.
   */
  chatDisabled: boolean;
  /** Whether to show the processing progress line (first-version processing). */
  showProgress: boolean;
  /** Whether to show the failure message + Refresh action (Requirement 6.4). */
  showFailure: boolean;
  /**
   * The active version still being shown after a failed refresh, or null. Non-
   * null only in {@link DetailPhase} `failed_showing_previous`; drives the
   * "still showing v{n}" line (Requirement 6.4).
   */
  stillShowingVersion: number | null;
  /** The latest progress/status message (`last_message`), or null. */
  message: string | null;
}

/** The inputs {@link deriveDetailPageState} needs, decoupled from the record. */
export interface DetailPageStateInput {
  /** The raw lifecycle status (`requested` / `processing` / `updated` / …). */
  status: string;
  /** The active version number, or null when no version is active yet. */
  activeVersion: number | null;
  /** The server-derived badge state (shared with the Library). */
  displayState: DatasetDetail["display_state"];
  /** The latest progress/status message, or null. */
  lastMessage: string | null;
}

/**
 * Derive the detail page's live state (Requirements 6.2 and 6.4).
 *
 * Pure: same inputs → same {@link DetailPageState}, with no DOM/time/I/O
 * dependency, so the component test and (later) the property test can exercise
 * it directly.
 *
 * Decision order:
 * 1. **Failed** first — a `failed` / `ready_refresh_failed` display state wins,
 *    because the analyst must see the failure and the Refresh action even
 *    though an earlier version may still be shown.
 * 2. **First-version processing** — raw status is `requested`/`processing` AND
 *    no active version: skeletons + progress + chat disabled.
 * 3. **Active** — anything else (a version is active; a refresh may be running,
 *    which {@link ReadinessBanner} annotates): fully usable.
 */
export function deriveDetailPageState({
  status,
  activeVersion,
  displayState,
  lastMessage,
}: DetailPageStateInput): DetailPageState {
  const message = lastMessage != null && lastMessage.trim().length > 0 ? lastMessage : null;

  // 1. Failed (Requirement 6.4). `failed` → no active version; the slots fall
  //    back to skeletons and the chat stays disabled. `ready_refresh_failed` →
  //    an earlier version is still active, so data + chat stay available and we
  //    add the "still showing v{n}" line.
  if (displayState === "failed" || displayState === "ready_refresh_failed") {
    const showingPrevious =
      displayState === "ready_refresh_failed" && activeVersion != null;
    return {
      phase: showingPrevious ? "failed_showing_previous" : "failed_no_version",
      showSkeletons: !showingPrevious,
      chatDisabled: !showingPrevious,
      showProgress: false,
      showFailure: true,
      stillShowingVersion: showingPrevious ? activeVersion : null,
      message,
    };
  }

  // 2. First-version processing (Requirement 6.2, first branch): status in
  //    flight AND no active version yet.
  if (IN_PROGRESS_STATUSES.has(status) && activeVersion == null) {
    return {
      phase: "first_version_processing",
      showSkeletons: true,
      chatDisabled: true,
      showProgress: true,
      showFailure: false,
      stillShowingVersion: null,
      message,
    };
  }

  // 3. Active (Requirement 6.2, second branch): a version is active — possibly
  //    while a refresh runs (`ready_refreshing`). Everything stays usable; the
  //    refreshing note is ReadinessBanner's job.
  return {
    phase: "active",
    showSkeletons: false,
    chatDisabled: false,
    showProgress: false,
    showFailure: false,
    stillShowingVersion: null,
    message,
  };
}

/** Convenience: derive the live state straight from a detail record. */
export function detailPageStateFor(dataset: DatasetDetail): DetailPageState {
  return deriveDetailPageState({
    status: dataset.status,
    activeVersion: dataset.active_version,
    displayState: dataset.display_state,
    lastMessage: dataset.last_message,
  });
}
