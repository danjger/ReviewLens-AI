/**
 * StreamingExchange — the live Q&A row + input wiring (guardrailed-chat task
 * 6.4, Requirements 5.5, 6.2, 6.3, 7.1).
 *
 * Sits at the bottom of the ChatPanel, below {@link HistoryPane}. It owns the
 * controlled question value and drives {@link useStreamingExchange}, rendering
 * the four phases of answering the next question:
 *
 * - **streaming** (Requirements 6.2, 7.1): the pending question and the live,
 *   token-by-token answer render here (below the history), and {@link ChatInput}
 *   is `pending` (disabled). The streaming answer is an `aria-live` region so a
 *   screen reader hears tokens as they arrive.
 * - **done (persisted)** (Requirement 6.3): the hook merges the Exchange into
 *   the history cache and returns to idle, so the row vanishes here and reappears
 *   in {@link HistoryPane}; the input clears and re-enables.
 * - **interrupted** (design "Error Handling"): a mid-stream failure keeps the
 *   partial answer and shows "Answer interrupted — retry" with a Retry button;
 *   nothing was saved.
 * - **unsaved** (Requirement 5.5): a `done` with `saved:false` shows the answer
 *   with a warning that it wasn't saved and a Retry that replays the signed
 *   Exchange to the save endpoint, flipping to saved (and into history) on
 *   success.
 *
 * The `conversation_id` is sourced inside the hook from the per-tab
 * `sessionStorage` helper; it is never shown here.
 *
 * ## Testing hooks (testing.md: data-testid / data-* only)
 *
 * The pending question, streaming answer, interrupted alert + retry, and unsaved
 * warning + retry each carry a `data-testid`. The phase is exposed on the root
 * as `data-phase` so tests assert transitions without matching copy, and never
 * assert on counts or dates.
 */
import { useState } from "react";

import ChatInput, { type ChatAvailability } from "./ChatInput";
import { useStreamingExchange } from "../hooks/useStreamingExchange";

export interface StreamingExchangeProps {
  /** The dataset to ask against, or null before it is known. */
  datasetId: string | null;
  /** The chat's availability, passed through to {@link ChatInput}. */
  availability: ChatAvailability;
  /**
   * Optionally control the input value from a parent (e.g. {@link ChatPanel},
   * so a {@link SuggestionChips} click can prefill the next question). When
   * both `value` and `onChange` are supplied the input is controlled by the
   * parent; otherwise this component owns the value internally (its original,
   * standalone behaviour).
   */
  value?: string;
  /** Called with the new text when controlled by a parent (see `value`). */
  onChange?: (value: string) => void;
}

export default function StreamingExchange({
  datasetId,
  availability,
  value: controlledValue,
  onChange: controlledOnChange,
}: StreamingExchangeProps) {
  // Controlled by the parent when both value + onChange are supplied, else this
  // component owns the input value (its original standalone behaviour).
  const [internalValue, setInternalValue] = useState("");
  const isControlled = controlledValue != null && controlledOnChange != null;
  const value = isControlled ? controlledValue : internalValue;
  const setValue = isControlled ? controlledOnChange : setInternalValue;
  const stream = useStreamingExchange(datasetId);

  function handleSubmit(questionText: string): void {
    // ChatInput already trimmed and validated. Clear the input as the stream
    // starts (Requirement 6.3: the input is cleared and the answer streams).
    setValue("");
    stream.start(questionText);
  }

  const showPendingRow =
    stream.phase === "streaming" ||
    stream.phase === "interrupted" ||
    stream.phase === "unsaved";

  return (
    <div className="streaming-exchange" data-testid="streaming-exchange" data-phase={stream.phase}>
      {showPendingRow && (
        <article className="streaming-exchange__row" data-testid="streaming-row">
          {stream.question != null && (
            <p
              className="streaming-exchange__question"
              data-testid="streaming-question"
            >
              {stream.question}
            </p>
          )}

          {/* The live answer: an aria-live region so tokens are announced as
              they stream in (Requirement 7.1 responsiveness + accessibility). */}
          <p
            className="streaming-exchange__answer"
            data-testid="streaming-answer"
            aria-live="polite"
            aria-busy={stream.phase === "streaming"}
          >
            {stream.answer}
          </p>

          {stream.phase === "interrupted" && (
            <div
              className="streaming-exchange__interrupted"
              data-testid="streaming-interrupted"
              role="alert"
            >
              <span className="streaming-exchange__interrupted-text">
                {stream.startError ?? "Answer interrupted — retry"}
              </span>
              <button
                type="button"
                className="streaming-exchange__retry"
                data-testid="streaming-interrupted-retry"
                onClick={stream.retryStream}
              >
                Retry
              </button>
            </div>
          )}

          {stream.phase === "unsaved" && (
            <div
              className="streaming-exchange__unsaved"
              data-testid="streaming-unsaved"
              role="alert"
            >
              <span className="streaming-exchange__unsaved-text">
                This answer wasn’t saved to the shared history.
              </span>
              {stream.retryError != null && (
                <span
                  className="streaming-exchange__unsaved-error"
                  data-testid="streaming-unsaved-error"
                >
                  {stream.retryError}
                </span>
              )}
              <button
                type="button"
                className="streaming-exchange__retry"
                data-testid="streaming-unsaved-retry"
                onClick={stream.retrySave}
                disabled={stream.savingRetry}
              >
                {stream.savingRetry ? "Saving…" : "Retry"}
              </button>
            </div>
          )}
        </article>
      )}

      <ChatInput
        value={value}
        onChange={setValue}
        onSubmit={handleSubmit}
        availability={availability}
        pending={stream.pending}
        rateLimitError={stream.rateLimitError}
      />
    </div>
  );
}
