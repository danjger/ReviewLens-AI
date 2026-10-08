/**
 * useStreamingExchange — owns the streaming Q&A lifecycle for one dataset
 * (guardrailed-chat task 6.4, Requirements 5.5, 6.2, 6.3, 7.1).
 *
 * This hook is the state machine behind {@link StreamingExchange}. It:
 *
 * - starts a stream for the next question through {@link streamChat}, sending
 *   the tab's `conversation_id` from {@link getConversationId} (Requirement 2.6);
 * - exposes the pending question and the live, token-by-token answer buffer so
 *   the pending Exchange renders at the bottom of the history while generating
 *   (Requirement 6.2, 7.1);
 * - on the `done` event, merges the final Exchange into the shared history
 *   cache (`chatHistoryQueryKey`) so it appears without a reload, then clears
 *   the pending state so the input re-enables (Requirement 6.3);
 * - on a mid-stream `error` event, keeps the partial answer and flips to an
 *   `interrupted` phase with a retry affordance; nothing was saved (design
 *   "Error Handling");
 * - when `done` carries `saved: false`, flips to an `unsaved` phase holding the
 *   signed Exchange, and {@link useStreamingExchange}'s `retrySave` replays it
 *   to the save endpoint, flipping to saved on success (Requirement 5.5);
 * - surfaces a `429` as a rate-limit error (for the input) and otherwise an
 *   error message, leaving the typed question intact so the analyst can retry.
 *
 * The merge-on-done writes into the SAME infinite-query cache
 * {@link useChatHistory} reads, appending the new Exchange to the newest page so
 * {@link flattenHistory} renders it last (newest). A `chat.exchange.saved`
 * realtime refetch (realtimeReducers) later reconciles it with the server copy;
 * because the ids match, the refetch replaces rather than duplicates.
 */
import {
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";

import {
  doneToExchange,
  postChatSave,
  streamChat,
  type HistoryPage,
  type SavedExchange,
} from "../api/chat";
import { ApiError } from "../api/ingest";
import { getConversationId } from "./conversationId";
import { chatHistoryQueryKey } from "./useChatHistory";

/** The streaming lifecycle phase (drives what {@link StreamingExchange} shows). */
export type StreamPhase =
  /** Nothing in flight; the input is enabled and no pending row shows. */
  | "idle"
  /** A stream is open; the pending question + live answer render. */
  | "streaming"
  /** A mid-stream error; the partial answer + "retry" affordance render. */
  | "interrupted"
  /** `done` arrived with `saved:false`; the answer + unsaved warning render. */
  | "unsaved";

/** The public state + actions of {@link useStreamingExchange}. */
export interface StreamingExchangeState {
  /** The current lifecycle phase. */
  phase: StreamPhase;
  /** The question being/last answered, or null when idle at the start. */
  question: string | null;
  /** The live answer buffer (grows token by token while streaming). */
  answer: string;
  /** True while a stream is open (the input is disabled, Requirement 6.3). */
  pending: boolean;
  /** The active rate-limit error (`429`), surfaced to the input, else null. */
  rateLimitError: ApiError | null;
  /** A non-rate-limit error message to show at the start affordance, else null. */
  startError: string | null;
  /** True while a save-retry request is in flight (disables the Retry button). */
  savingRetry: boolean;
  /** The last save-retry's error message, when it failed, else null. */
  retryError: string | null;
  /** Start streaming an answer for `question` (no-op while already pending). */
  start: (question: string) => void;
  /** Retry the stream after a mid-stream interruption (same question). */
  retryStream: () => void;
  /** Retry saving the current unsaved Exchange (Requirement 5.5). */
  retrySave: () => void;
  /** Reset to idle (e.g. after merging) — clears the pending/answer state. */
  reset: () => void;
}

/**
 * Append a just-answered Exchange to the newest page of the dataset's history
 * infinite-query cache, so it renders at the bottom of the pane on `done`
 * (merge-on-done, Requirement 6.3) without a refetch.
 *
 * The newest page is `pages[0]` (the hook pages backward: page 0 is the end of
 * the timeline), and items within a page are oldest-first, so the new Exchange
 * is pushed to the END of page 0. If the cache is empty (history not loaded
 * yet), a single-page cache is seeded so the Exchange still shows.
 *
 * Idempotent: an Exchange whose id is already present is not added again, so a
 * later `chat.exchange.saved` refetch (or a double `done`) can't duplicate it.
 */
function mergeExchangeIntoCache(
  client: ReturnType<typeof useQueryClient>,
  datasetId: string,
  payload: SavedExchange,
): void {
  const key = chatHistoryQueryKey(datasetId);
  const exchange = doneToExchange(payload);

  client.setQueryData<InfiniteData<HistoryPage, string | null>>(key, (data) => {
    if (data == null) {
      // History not loaded yet: seed a single newest page holding the Exchange.
      return {
        pages: [{ items: [exchange], next_before: null, has_more: false }],
        pageParams: [null],
      };
    }
    const alreadyPresent = data.pages.some((page) =>
      page.items.some((item) => item.type === "exchange" && item.id === exchange.id),
    );
    if (alreadyPresent) return data;

    const [newest, ...rest] = data.pages;
    const mergedNewest: HistoryPage = {
      ...newest,
      items: [...newest.items, exchange],
    };
    return { ...data, pages: [mergedNewest, ...rest] };
  });
}

/**
 * Create the streaming-exchange state machine for a dataset.
 *
 * @param datasetId - the dataset to ask against, or null before it is known
 *   (while null, `start` is a no-op).
 */
export function useStreamingExchange(datasetId: string | null): StreamingExchangeState {
  const client = useQueryClient();

  const [phase, setPhase] = useState<StreamPhase>("idle");
  const [question, setQuestion] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const [rateLimitError, setRateLimitError] = useState<ApiError | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [savingRetry, setSavingRetry] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  // The signed Exchange from a `saved:false` done, held for the save-retry.
  const unsavedExchangeRef = useRef<SavedExchange | null>(null);
  // Abort the in-flight stream on reset/unmount so handlers stop firing.
  const abortRef = useRef<AbortController | null>(null);
  // Guard against overlapping streams: only one answer generates at a time.
  const pendingRef = useRef(false);

  const reset = useCallback((): void => {
    abortRef.current?.abort();
    abortRef.current = null;
    pendingRef.current = false;
    unsavedExchangeRef.current = null;
    setPhase("idle");
    setQuestion(null);
    setAnswer("");
    setStartError(null);
    setRetryError(null);
    setSavingRetry(false);
  }, []);

  const runStream = useCallback(
    (q: string): void => {
      if (datasetId == null) return;
      if (pendingRef.current) return; // one answer at a time (Requirement 6.3)

      pendingRef.current = true;
      unsavedExchangeRef.current = null;
      setQuestion(q);
      setAnswer("");
      setStartError(null);
      setRetryError(null);
      setRateLimitError(null);
      setPhase("streaming");

      const controller = new AbortController();
      abortRef.current = controller;

      void streamChat(
        datasetId,
        { question: q, conversation_id: getConversationId() },
        {
          onToken: (text) => setAnswer((prev) => prev + text),
          onDone: (exchange) => {
            pendingRef.current = false;
            abortRef.current = null;
            if (exchange.saved) {
              // Persisted: merge into history and return to idle so the input
              // clears and re-enables (Requirement 6.3). The pending row is
              // replaced by the merged history Exchange.
              mergeExchangeIntoCache(client, datasetId, exchange);
              setPhase("idle");
              setQuestion(null);
              setAnswer("");
            } else {
              // Save failed: keep the answer visible with an unsaved warning +
              // Retry (Requirement 5.5). Hold the signed payload for the retry.
              unsavedExchangeRef.current = exchange;
              setAnswer(exchange.answer);
              setPhase("unsaved");
            }
          },
          onError: () => {
            // Mid-stream failure: keep the partial answer, offer a retry; the
            // server saved nothing (design "Error Handling").
            pendingRef.current = false;
            abortRef.current = null;
            setPhase("interrupted");
          },
        },
        controller.signal,
      ).catch((err: unknown) => {
        // A pre-stream HTTP failure (429/409/other) or a network error.
        pendingRef.current = false;
        abortRef.current = null;
        if (err instanceof ApiError && err.isRateLimited) {
          // Surface to the input's rate-limit state; drop the pending row.
          setRateLimitError(err);
          setPhase("idle");
          setQuestion(null);
          setAnswer("");
          return;
        }
        const message =
          err instanceof ApiError
            ? err.message
            : "Something went wrong starting the answer.";
        setStartError(message);
        setPhase("interrupted");
      });
    },
    [client, datasetId],
  );

  const start = useCallback(
    (q: string): void => {
      runStream(q);
    },
    [runStream],
  );

  const retryStream = useCallback((): void => {
    if (question == null) return;
    runStream(question);
  }, [question, runStream]);

  const retrySave = useCallback((): void => {
    if (datasetId == null) return;
    const exchange = unsavedExchangeRef.current;
    if (exchange == null || savingRetry) return;

    setSavingRetry(true);
    setRetryError(null);
    void postChatSave(datasetId, exchange)
      .then(() => {
        // Saved on retry: merge into history and return to idle. The
        // `chat.exchange.saved` broadcast will also refetch, but the ids match
        // so the merge is reconciled rather than duplicated.
        mergeExchangeIntoCache(client, datasetId, exchange);
        unsavedExchangeRef.current = null;
        setSavingRetry(false);
        setPhase("idle");
        setQuestion(null);
        setAnswer("");
      })
      .catch((err: unknown) => {
        setSavingRetry(false);
        setRetryError(
          err instanceof ApiError ? err.message : "Saving failed. Try again.",
        );
      });
  }, [client, datasetId, savingRetry]);

  return {
    phase,
    question,
    answer,
    pending: phase === "streaming",
    rateLimitError,
    startError,
    savingRetry,
    retryError,
    start,
    retryStream,
    retrySave,
    reset,
  };
}
