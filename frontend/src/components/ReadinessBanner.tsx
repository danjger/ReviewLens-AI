/**
 * ReadinessBanner — the "is this dataset ready for analysis" indicator at the
 * top of the detail page (ingestion-summary task 4.3, Requirement 4.5).
 *
 * The analyst needs a single, trustworthy signal of whether the active version
 * is safe to draw conclusions from before asking the chat questions. This
 * banner derives that signal from the active version, its warnings and review
 * count, and the dataset's `display_state` (design "Components and Interfaces":
 * "`ReadinessBanner`: derives its state from `active_version`, its warnings and
 * review count, and `display_state` for the refreshing and refresh-failed
 * notes").
 *
 * **The rules (Requirement 4.5).** For the active version:
 *
 * - **"Ready for analysis"** — it has no warnings AND at least
 *   {@link MIN_REVIEWS_FOR_READY} (20) reviews.
 * - **"Ready with caveats"** — it has warnings OR fewer than 20 reviews.
 * - **"Not ready"** — there is no active version.
 *
 * **The notes (Requirement 4.5).** WHILE a refresh is running
 * (`display_state === "ready_refreshing"`) OR after a refresh failed
 * (`display_state === "ready_refresh_failed"`), the indicator adds a note that
 * names the active version being shown:
 *
 * - refreshing → "Refreshing — showing v{active} until v{active + 1} is ready"
 * - refresh failed → "Last refresh failed — showing v{active}"
 *
 * The note only applies when there *is* an active version (both states imply
 * one); with no active version the level is "Not ready" and there is no note.
 *
 * **Correctness Property 2 ("Readiness follows the rules").** The derivation is
 * a pure function, {@link deriveReadiness}, exported so the property test
 * (task 9) can reuse it directly rather than scraping the DOM. The component is
 * a thin render of that function's output.
 *
 * **Data source.** The warnings and review count come from the active-version
 * `metrics` dict produced by review-analysis
 * (`app/handlers/metrics.py::compute_metrics`, mirrored in the `makeMetrics`
 * fixture): `metrics.warnings` (a string array) and `metrics.review_count` (a
 * number). The field names mirror that producer exactly, consistent with
 * {@link CompletenessPanel} and {@link MetricsPanel}.
 */
import type { DatasetDetail, DisplayState } from "../api/datasets";

/**
 * The review-count threshold at or above which a warning-free active version is
 * "Ready for analysis" (Requirement 4.5: "at least 20 reviews").
 */
export const MIN_REVIEWS_FOR_READY = 20;

/** The three readiness levels (Requirement 4.5). */
export type ReadinessLevel = "ready" | "ready_with_caveats" | "not_ready";

/** The kind of note a refresh state adds, or none. */
export type ReadinessNoteKind = "refreshing" | "refresh_failed" | null;

/** The fully-derived readiness state the banner renders. */
export interface Readiness {
  /** Which of the three threshold rules applies. */
  level: ReadinessLevel;
  /** The human label for {@link Readiness.level}. */
  label: string;
  /** Which refresh note applies (if any). */
  note: ReadinessNoteKind;
  /** The rendered note text, or null when there is no note. */
  noteText: string | null;
  /** The active version the banner is describing, or null when none. */
  activeVersion: number | null;
}

/** The human label for each readiness level (Requirement 4.5 wording). */
const LEVEL_LABELS: Record<ReadinessLevel, string> = {
  ready: "Ready for analysis",
  ready_with_caveats: "Ready with caveats",
  not_ready: "Not ready",
};

/** True when *value* is a finite number (so `0` counts, `NaN` does not). */
function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Read a field off the (untyped) metrics record, or `undefined` when absent. */
function field(metrics: DatasetDetail["metrics"], key: string): unknown {
  if (metrics == null) return undefined;
  return (metrics as Record<string, unknown>)[key];
}

/**
 * The count of warnings on the active version. Only non-empty strings count, so
 * a stray empty entry can never force "Ready with caveats"; a missing or
 * malformed `warnings` field is treated as zero warnings. Mirrors
 * {@link CompletenessPanel}'s `readWarnings`.
 */
function warningCount(metrics: DatasetDetail["metrics"]): number {
  const raw = field(metrics, "warnings");
  if (!Array.isArray(raw)) return 0;
  return raw.filter(
    (w) => typeof w === "string" && w.trim().length > 0,
  ).length;
}

/**
 * The active version's review count, or `null` when the field is missing or not
 * a finite number. A `null` count is treated as "fewer than 20" below — an
 * active version whose count we cannot read is not confidently "Ready for
 * analysis".
 */
function reviewCount(metrics: DatasetDetail["metrics"]): number | null {
  const raw = field(metrics, "review_count");
  return isNumber(raw) ? raw : null;
}

/** The refresh note (if any) implied by a `display_state`. */
function noteKindFor(displayState: DisplayState): ReadinessNoteKind {
  if (displayState === "ready_refreshing") return "refreshing";
  if (displayState === "ready_refresh_failed") return "refresh_failed";
  return null;
}

/** Render the note text for a given note kind and active version. */
function noteTextFor(
  note: ReadinessNoteKind,
  activeVersion: number,
): string | null {
  if (note === "refreshing") {
    return `Refreshing — showing v${activeVersion} until v${activeVersion + 1} is ready`;
  }
  if (note === "refresh_failed") {
    return `Last refresh failed — showing v${activeVersion}`;
  }
  return null;
}

/** The inputs {@link deriveReadiness} needs, decoupled from `DatasetDetail`. */
export interface ReadinessInput {
  /** The active version number, or null when no version is active yet. */
  activeVersion: number | null;
  /** The server-derived badge state (drives the refresh notes). */
  displayState: DisplayState;
  /** The active-version metrics dict (warnings + review count), or null. */
  metrics: DatasetDetail["metrics"];
}

/**
 * Derive the readiness state from the active version, its metrics, and the
 * display state (Requirement 4.5, Correctness Property 2).
 *
 * Pure: given the same inputs it always returns the same {@link Readiness},
 * with no DOM, time, or I/O dependency — so the property test can exercise it
 * directly over generated inputs.
 *
 * Rule order:
 * 1. No active version → "Not ready" (and never a note; a note implies a shown
 *    version).
 * 2. Otherwise apply the warnings/count thresholds for the level.
 * 3. Layer the refresh note from the display state onto that level.
 */
export function deriveReadiness({
  activeVersion,
  displayState,
  metrics,
}: ReadinessInput): Readiness {
  // Rule: no active version → "Not ready", no note.
  if (activeVersion == null) {
    return {
      level: "not_ready",
      label: LEVEL_LABELS.not_ready,
      note: null,
      noteText: null,
      activeVersion: null,
    };
  }

  const warnings = warningCount(metrics);
  const count = reviewCount(metrics);
  const hasEnoughReviews = count != null && count >= MIN_REVIEWS_FOR_READY;

  // "Ready for analysis" only when there are no warnings AND at least 20
  // reviews; any warning, or too few (or unknown) reviews, means "caveats".
  const level: ReadinessLevel =
    warnings === 0 && hasEnoughReviews ? "ready" : "ready_with_caveats";

  const note = noteKindFor(displayState);
  const noteText = noteTextFor(note, activeVersion);

  return {
    level,
    label: LEVEL_LABELS[level],
    note,
    noteText,
    activeVersion,
  };
}

export interface ReadinessBannerProps {
  /** The full detail record for the dataset being shown. */
  dataset: DatasetDetail;
}

/**
 * Render the readiness indicator for a dataset's active version. A thin view
 * over {@link deriveReadiness}; all of the rule logic lives in that pure
 * function so it stays testable in isolation.
 */
export default function ReadinessBanner({ dataset }: ReadinessBannerProps) {
  const readiness = deriveReadiness({
    activeVersion: dataset.active_version,
    displayState: dataset.display_state,
    metrics: dataset.metrics,
  });

  return (
    <section
      className={`readiness-banner readiness-banner--${readiness.level}`}
      data-testid="readiness-banner"
      data-state={readiness.level}
      aria-label="Readiness for analysis"
      role="status"
    >
      <p className="readiness-banner__level" data-testid="readiness-level">
        {readiness.label}
      </p>
      {readiness.noteText != null && (
        <p
          className="readiness-banner__note"
          data-testid="readiness-note"
          data-note={readiness.note ?? undefined}
        >
          {readiness.noteText}
        </p>
      )}
    </section>
  );
}
