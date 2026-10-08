/**
 * VersionNote — the "which version answers come from" note shown above the
 * ChatInput while a refresh is running or after one failed (guardrailed-chat
 * task 6.3, Requirement 1.1).
 *
 * The chat stays usable during and after a refresh; it keeps answering from the
 * current active version. This note tells the analyst that explicitly:
 *
 * - a refresh in flight  → "Answers use v{active} until the refresh finishes"
 * - the last refresh failed → "Last refresh failed — answers use v{active}"
 *
 * In the normal state (no refresh running, none failed) it renders nothing.
 *
 * The parent ChatPanel derives both the `state` and the `activeVersion` from
 * the same {@link ChatAvailability} mapping that drives {@link ChatInput} (see
 * ChatInput's module docstring): `refreshing` and `refresh_failed` show the
 * note; `available`, `no_active_version`, and `archived` show nothing. Keeping
 * the note a separate component lets the parent place it above the input while
 * ChatInput stays focused on the input itself.
 *
 * Testing hooks (testing.md: data-testid / data-* only): the note carries a
 * `data-testid` and exposes the active version as `data-active-version` so
 * tests assert the version without matching the formatted string.
 */

/** The refresh-driven states that produce a note (plus `none` for no note). */
export type VersionNoteState = "refreshing" | "refresh_failed" | "none";

export interface VersionNoteProps {
  /** The version answers currently come from (the dataset's `active_version`). */
  activeVersion: number;
  /** Whether a refresh is in flight, the last one failed, or neither. */
  state: VersionNoteState;
}

export default function VersionNote({ activeVersion, state }: VersionNoteProps) {
  if (state === "none") return null;

  const text =
    state === "refreshing"
      ? `Answers use v${activeVersion} until the refresh finishes`
      : `Last refresh failed — answers use v${activeVersion}`;

  return (
    <p
      className="version-note"
      data-testid="version-note"
      data-state={state}
      data-active-version={activeVersion}
      role="status"
    >
      {text}
    </p>
  );
}
