/**
 * ChatInput — the question textarea at the bottom of the ChatPanel
 * (guardrailed-chat task 6.3, Requirements 1.1, 1.2, 1.3, 6.1, 6.4).
 *
 * A controlled, presentational input for the analyst's next question. It owns
 * the input rules and the enabled/disabled presentation, and nothing else: the
 * wiring to the streaming SSE client, clearing on completion, and the pending
 * (streaming) lifecycle live in {@link StreamingExchange} (task 6.4). This
 * component just:
 *
 * - renders the textarea + character counter (≤ 1,000 chars, Requirement 6.1),
 * - submits a *validated* question through {@link ChatInputProps.onSubmit}
 *   (Enter submits, Shift+Enter inserts a newline — Requirement 6.1),
 * - rejects empty / whitespace-only questions on the client (Requirement 6.4),
 * - reflects a {@link ChatAvailability} state for the disabled cases and copy
 *   (Requirements 1.2, 1.3) and a `pending` flag while an answer streams
 *   (Requirement 6.2 — the streaming state is driven by 6.4),
 * - shows a 429 "ask again" time when rate-limited (design "Error Handling"),
 *   reusing {@link RateLimitMessage}'s wording via a shared resume phrase.
 *
 * ## Availability model
 *
 * The parent ChatPanel (future) computes a single {@link ChatAvailability} from
 * the dataset detail and passes it in. The mapping (from `active_version`,
 * `archived_at`/archived, and `display_state`) is:
 *
 * | Availability        | When                                                        | Input    | Note / message                                    |
 * |---------------------|-------------------------------------------------------------|----------|---------------------------------------------------|
 * | `available`         | has `active_version`, not archived, no refresh in flight    | enabled  | — (no VersionNote)                                |
 * | `refreshing`        | has `active_version`, not archived, a refresh is running    | enabled  | VersionNote: "Answers use v{n} until the refresh finishes" |
 * | `refresh_failed`    | has `active_version`, not archived, the last refresh failed | enabled  | VersionNote: "Last refresh failed — answers use v{n}" |
 * | `no_active_version` | no `active_version` (first processing unfinished or failed) | disabled | explanatory message (Requirement 1.2)             |
 * | `archived`          | archived                                                    | disabled | "Restore this dataset to ask new questions." (Req 1.3) |
 *
 * `available`, `refreshing`, and `refresh_failed` all keep the chat usable
 * (Requirement 1.1: the chat stays available during and after a refresh); they
 * differ only in the {@link VersionNote} shown above the input, which is a
 * separate component the parent renders from the same availability + version.
 *
 * ChatInput only needs to know whether the state is *enabled* and, when not,
 * which message to show, so it derives both from {@link ChatAvailability}.
 *
 * ## Testing hooks (testing.md: data-testid / data-* only)
 *
 * The textarea, counter, submit button, disabled message, and rate-limit
 * message each carry a `data-testid`. The live character count is exposed as a
 * `data-count` attribute on the counter so tests assert the count without
 * matching formatted text.
 */
import {
  useId,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
} from "react";

import type { ApiError } from "../api/ingest";
import { chatResumePhrase } from "./RateLimitMessage";

/**
 * The chat's availability, derived by the parent from the dataset detail. See
 * the mapping table in the module docstring. ChatInput is enabled only for the
 * three "has an active version and not archived" states.
 */
export type ChatAvailability =
  | "available"
  | "refreshing"
  | "refresh_failed"
  | "no_active_version"
  | "archived";

/** The maximum question length accepted by the input (Requirement 6.1). */
export const MAX_QUESTION_LENGTH = 1000;

/** The enabled-input placeholder (Requirement 1.1). */
export const CHAT_PLACEHOLDER = "Ask a question about these reviews…";

/** The archived disabled message (Requirement 1.3, verbatim). */
export const ARCHIVED_MESSAGE = "Restore this dataset to ask new questions.";

/** The "no active version" disabled message (Requirement 1.2). */
export const NO_ACTIVE_VERSION_MESSAGE =
  "The chat opens when the first version of this dataset finishes processing.";

/** True when the availability state keeps the input enabled. */
export function isChatEnabled(availability: ChatAvailability): boolean {
  return (
    availability === "available" ||
    availability === "refreshing" ||
    availability === "refresh_failed"
  );
}

/**
 * The disabled message for a non-enabled availability state, or null when the
 * state is enabled (so no message is shown).
 */
export function disabledMessage(availability: ChatAvailability): string | null {
  switch (availability) {
    case "archived":
      return ARCHIVED_MESSAGE;
    case "no_active_version":
      return NO_ACTIVE_VERSION_MESSAGE;
    default:
      return null;
  }
}

/** True when `question` is a submittable (non-empty, non-whitespace) string. */
export function isSubmittableQuestion(question: string): boolean {
  const trimmed = question.trim();
  return trimmed.length > 0 && trimmed.length <= MAX_QUESTION_LENGTH;
}

export interface ChatInputProps {
  /** Current question text (controlled by the parent — task 6.4). */
  value: string;
  /** Called with the new text on every edit (already length-capped here). */
  onChange: (value: string) => void;
  /**
   * Called with the trimmed, validated question when the analyst submits. The
   * parent (task 6.4) wires this to the SSE client and clears `value` once the
   * stream starts; ChatInput never clears or streams on its own.
   */
  onSubmit: (question: string) => void;
  /** The chat's availability (drives enabled/disabled + which message shows). */
  availability: ChatAvailability;
  /**
   * True while an answer is streaming (Requirement 6.2). The input is disabled
   * but shows no disabled *message* — the streaming answer appears in the
   * history (task 6.4), not here.
   */
  pending?: boolean;
  /**
   * The active rate-limit error, when the last submit was refused with `429`
   * (design "Error Handling"). While set, the input is disabled and the resume
   * time is shown; the parent clears it to re-enable (e.g. after `Retry-After`
   * elapses or on the next successful request).
   */
  rateLimitError?: ApiError | null;
}

export default function ChatInput({
  value,
  onChange,
  onSubmit,
  availability,
  pending = false,
  rateLimitError = null,
}: ChatInputProps) {
  const textareaId = useId();
  const [attemptedEmpty, setAttemptedEmpty] = useState(false);

  const enabledByAvailability = isChatEnabled(availability);
  const rateLimited = rateLimitError != null;
  // Disabled while unavailable, while an answer streams, or while rate-limited.
  const disabled = !enabledByAvailability || pending || rateLimited;

  const canSubmit = !disabled && isSubmittableQuestion(value);

  const availabilityMessage = disabledMessage(availability);

  function trySubmit(): void {
    if (disabled) return;
    if (!isSubmittableQuestion(value)) {
      // Empty / whitespace-only guard (Requirement 6.4): never fire onSubmit.
      setAttemptedEmpty(true);
      return;
    }
    setAttemptedEmpty(false);
    onSubmit(value.trim());
  }

  function handleChange(event: ChangeEvent<HTMLTextAreaElement>): void {
    // Enforce the 1,000-character limit at the source so neither the counter
    // nor onSubmit can ever exceed it (Requirement 6.1).
    const next = event.target.value.slice(0, MAX_QUESTION_LENGTH);
    if (attemptedEmpty && next.trim().length > 0) setAttemptedEmpty(false);
    onChange(next);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    // Enter submits; Shift+Enter inserts a newline (Requirement 6.1). Ignore
    // IME composition so Enter confirming a composition doesn't submit.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      trySubmit();
    }
  }

  return (
    <form
      className="chat-input"
      data-testid="chat-input"
      data-availability={availability}
      onSubmit={(event) => {
        event.preventDefault();
        trySubmit();
      }}
    >
      <label className="chat-input__label" htmlFor={textareaId}>
        Ask a question about these reviews
      </label>
      <textarea
        id={textareaId}
        className="chat-input__textarea"
        data-testid="chat-input-textarea"
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        disabled={disabled}
        maxLength={MAX_QUESTION_LENGTH}
        rows={3}
        placeholder={enabledByAvailability ? CHAT_PLACEHOLDER : undefined}
        aria-describedby={availabilityMessage != null ? `${textareaId}-msg` : undefined}
      />

      <div className="chat-input__footer">
        <span
          className="chat-input__counter"
          data-testid="chat-input-counter"
          data-count={value.length}
          data-max={MAX_QUESTION_LENGTH}
        >
          {value.length}/{MAX_QUESTION_LENGTH}
        </span>
        <button
          type="submit"
          className="chat-input__submit"
          data-testid="chat-input-submit"
          disabled={!canSubmit}
        >
          {pending ? "Answering…" : "Ask"}
        </button>
      </div>

      {attemptedEmpty && (
        <p className="chat-input__empty" data-testid="chat-input-empty" role="alert">
          Enter a question to ask.
        </p>
      )}

      {availabilityMessage != null && (
        <p
          id={`${textareaId}-msg`}
          className="chat-input__disabled-message"
          data-testid="chat-input-disabled-message"
          data-availability={availability}
          role="status"
        >
          {availabilityMessage}
        </p>
      )}

      {rateLimited && (
        <p className="chat-input__rate-limit" data-testid="chat-input-rate-limit" role="alert">
          Too many questions right now. {chatResumePhrase(rateLimitError.retryAfterSeconds)}
        </p>
      )}
    </form>
  );
}
