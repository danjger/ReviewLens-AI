/**
 * chatAvailability — derive the chat panel's {@link ChatAvailability} from a
 * dataset detail record (guardrailed-chat task 9; the mapping documented in
 * {@link ChatInput}'s module docstring).
 *
 * {@link ChatPanel} / {@link ChatInput} take a single {@link ChatAvailability}
 * and render the enabled/disabled input, the version note, and the disabled
 * copy from it. The mapping from the dataset detail's `archived_at`,
 * `active_version`, and `display_state` lives here — one pure function — so the
 * host (the {@link DatasetDetailPage}) that mounts the panel stays a thin wiring
 * layer and the rule is unit-testable in isolation.
 *
 * The mapping (verbatim from the ChatInput docstring table):
 *
 * | Availability        | When                                                        |
 * |---------------------|-------------------------------------------------------------|
 * | `archived`          | the dataset is archived (`archived_at != null`)             |
 * | `no_active_version` | no `active_version` (first processing unfinished or failed) |
 * | `refreshing`        | has `active_version`, `display_state === "ready_refreshing"` |
 * | `refresh_failed`    | has `active_version`, `display_state === "ready_refresh_failed"` |
 * | `available`         | has `active_version`, not archived, no refresh in flight    |
 *
 * Decision order matters: `archived` wins over everything (an archived dataset
 * is read-only even if it has an active version — Requirement 1.3), then the
 * "no active version" guard (Requirement 1.2), then the refresh states, then
 * the plain available state. This mirrors {@link deriveDetailPageState}'s
 * precedence (archived/failed first), so the panel and the summary agree.
 */
import type { DatasetDetail } from "../api/datasets";
import type { ChatAvailability } from "./ChatInput";

/** The inputs {@link deriveChatAvailability} needs, decoupled from the record. */
export interface ChatAvailabilityInput {
  /** Whether the dataset is archived (`archived_at != null`). */
  archived: boolean;
  /** The active version number, or null when no version is active yet. */
  activeVersion: number | null;
  /** The server-derived badge state (shared with the Library). */
  displayState: DatasetDetail["display_state"];
}

/**
 * Derive the chat availability (Requirement 1). Pure: same inputs → same
 * {@link ChatAvailability}, with no DOM/time/I-O dependency, so the component
 * test and (later) the property test can exercise it directly.
 */
export function deriveChatAvailability({
  archived,
  activeVersion,
  displayState,
}: ChatAvailabilityInput): ChatAvailability {
  // 1. Archived wins: read-only, input disabled (Requirement 1.3).
  if (archived) return "archived";

  // 2. No active version to ground answers in: input disabled (Requirement 1.2).
  if (activeVersion == null) return "no_active_version";

  // 3. A version is active; a refresh may be running or have just failed. The
  //    chat stays usable in both — only the VersionNote differs (Requirement 1.1).
  if (displayState === "ready_refreshing") return "refreshing";
  if (displayState === "ready_refresh_failed") return "refresh_failed";

  // 4. Plain available: an active version, not archived, no refresh in flight.
  return "available";
}

/** Convenience: derive the chat availability straight from a detail record. */
export function chatAvailabilityFor(dataset: DatasetDetail): ChatAvailability {
  return deriveChatAvailability({
    archived: dataset.archived_at != null,
    activeVersion: dataset.active_version,
    displayState: dataset.display_state,
  });
}
