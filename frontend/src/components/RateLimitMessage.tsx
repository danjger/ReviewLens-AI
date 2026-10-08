/**
 * RateLimitMessage — the 429 notice telling the analyst when checks can resume
 * (Requirement 1.6).
 *
 * When the backend rate-limits a Check (per-IP or global), it returns `429`
 * with a `Retry-After` header. {@link ApiError} parses that into
 * `retryAfterSeconds`; this component turns it into a plain-language resume
 * time. When the delay is unknown, it falls back to a generic message.
 */
import type { ApiError } from "../api/ingest";

export interface RateLimitMessageProps {
  error: ApiError;
}

/**
 * Phrase a `Retry-After` seconds delay as a resume time for a given action
 * (e.g. "check more URLs", "ask again"). Shared by {@link RateLimitMessage}
 * (Check) and {@link ChatInput} (guardrailed-chat task 6.3, design "Error
 * Handling") so the two render the same wording from the same `Retry-After`.
 *
 * - `null`/non-positive → a generic "wait a moment" fallback,
 * - under a minute → whole seconds,
 * - a minute or more → minutes rounded up (singular at exactly one minute).
 */
export function resumePhraseFor(action: string, seconds: number | null): string {
  if (seconds == null || seconds <= 0) {
    return `Please wait a moment before you ${action} again.`;
  }
  if (seconds < 60) {
    return `You can ${action} in ${seconds} seconds.`;
  }
  const minutes = Math.ceil(seconds / 60);
  return `You can ${action} in about ${minutes} minute${minutes === 1 ? "" : "s"}.`;
}

/** The chat "ask again" resume phrase (guardrailed-chat task 6.3). */
export function chatResumePhrase(seconds: number | null): string {
  return resumePhraseFor("ask", seconds);
}

/** Phrase a seconds delay as "in N seconds" / "in about N minutes". */
function resumePhrase(seconds: number | null): string {
  if (seconds == null || seconds <= 0) {
    return "Please wait a moment before checking more URLs.";
  }
  if (seconds < 60) {
    return `You can check more URLs in ${seconds} seconds.`;
  }
  const minutes = Math.ceil(seconds / 60);
  return `You can check more URLs in about ${minutes} minute${minutes === 1 ? "" : "s"}.`;
}

export default function RateLimitMessage({ error }: RateLimitMessageProps) {
  return (
    <p className="rate-limit-message" data-testid="rate-limit-message" role="alert">
      Too many URL checks right now. {resumePhrase(error.retryAfterSeconds)}
    </p>
  );
}
