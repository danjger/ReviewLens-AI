/**
 * ChatPanel — the guardrailed Q&A panel that composes the 6.1–6.8 pieces
 * (guardrailed-chat task 6; design "Frontend (ChatPanel)").
 *
 * The design describes a single `ChatPanel` that stacks, top to bottom:
 *
 *   HistoryPane (6.1)                — the shared Q&A timeline with refresh
 *                                      markers (6.6), stale chips (6.6), and the
 *                                      history toolbar (6.7);
 *   SuggestionChips (6.5)            — shown prominently when the history is
 *                                      empty, else as a collapsed row above the
 *                                      input;
 *   VersionNote (6.3)                — "Answers use v{n}…" while a refresh runs
 *                                      or after one failed;
 *   StreamingExchange (6.4)          — the live pending question + streaming
 *                                      answer and the ChatInput (6.3).
 *
 * The individual pieces were built and focus-tested in 6.1–6.8. This container
 * is the thin composition that wires them together and owns exactly two bits of
 * shared state the pieces don't each own:
 *
 * 1. **The controlled input value**, so {@link SuggestionChips} can *prefill*
 *    the next question (not auto-submit — see SuggestionChips' docstring) by
 *    lifting the chosen text into the value {@link StreamingExchange} controls.
 * 2. **Whether the history is empty**, read from the same
 *    {@link useChatHistory} cache the pane renders, to drive the suggestions'
 *    empty-vs-collapsed presentation.
 *
 * ## Live refresh markers (task 6.8, Requirements 9.2, 9.7)
 *
 * The panel mounts the tab's single real-time channel ({@link useRealtime}),
 * so a `dataset.status.changed` frame for this dataset flows through the
 * realtime reducer, which invalidates this dataset's chat-history query; the
 * pane then refetches and the pending {@link RefreshMarker} appears or changes
 * state (pending → completed/failed) without a reload. The `useRealtime` mount
 * is idempotent per tab (the detail route may also mount it), so composing it
 * here is safe; a `realtimeEnabled` flag lets a host that already owns the
 * channel (or a test driving the cache directly) opt out.
 *
 * ## Availability (task 6.3, Requirement 1)
 *
 * The host passes a single {@link ChatAvailability} (and, when a refresh is
 * running or failed, the active version), derived from the dataset detail per
 * the mapping in {@link ChatInput}'s docstring. The panel renders the
 * {@link VersionNote} from the same availability + version and hands the
 * availability to {@link StreamingExchange} → {@link ChatInput}. This keeps the
 * detail→availability mapping in one place (the host) and the panel purely a
 * composition.
 *
 * ## Testing hooks (testing.md: data-testid / data-* only)
 *
 * The root carries `data-testid="chat-panel"` and reflects the availability as
 * `data-availability`. All inner behaviour is tested through the children's own
 * `data-testid`s (history rows, refresh markers, the input, suggestion chips).
 */
import { useMemo, useState } from "react";

import { useChatHistory, flattenHistory } from "../hooks/useChatHistory";
import { useRealtime } from "../hooks/useRealtime";
import { isExchange } from "../api/chat";
import { type ChatAvailability } from "./ChatInput";
import StreamingExchange from "./StreamingExchange";
import SuggestionChips from "./SuggestionChips";
import HistoryPane from "./HistoryPane";
import VersionNote, { type VersionNoteState } from "./VersionNote";

export interface ChatPanelProps {
  /** The dataset to ask about, or null before its id is known. */
  datasetId: string | null;
  /**
   * The chat's availability, derived by the host from the dataset detail (see
   * {@link ChatInput}'s mapping). Drives the input's enabled/disabled state and
   * which {@link VersionNote}, if any, shows. Defaults to `available`.
   */
  availability?: ChatAvailability;
  /**
   * The version answers currently come from (the dataset's `active_version`),
   * used by the {@link VersionNote} when a refresh is running or failed. Only
   * read for the `refreshing` / `refresh_failed` availabilities.
   */
  activeVersion?: number | null;
  /**
   * Open the tab's real-time channel so refresh markers update live (task 6.8).
   * Default true; set false in a host that already mounts {@link useRealtime}
   * or in a test driving the cache directly.
   */
  realtimeEnabled?: boolean;
}

/** Map an availability to the {@link VersionNote} state (none → no note). */
function versionNoteState(availability: ChatAvailability): VersionNoteState {
  if (availability === "refreshing") return "refreshing";
  if (availability === "refresh_failed") return "refresh_failed";
  return "none";
}

export default function ChatPanel({
  datasetId,
  availability = "available",
  activeVersion = null,
  realtimeEnabled = true,
}: ChatPanelProps) {
  // The tab's single live channel: a `dataset.status.changed` frame for this
  // dataset refetches the history so refresh markers go live (task 6.8).
  useRealtime({ enabled: realtimeEnabled });

  // The controlled question value, shared so a suggestion chip can prefill it
  // (SuggestionChips → onSelect) and StreamingExchange's input reads/edits it.
  const [value, setValue] = useState("");

  // Read the same history cache the pane renders to know whether the timeline
  // has any Exchanges yet — the suggestions' empty-vs-collapsed presentation
  // keys off this (design "Frontend (ChatPanel)").
  const historyQuery = useChatHistory(datasetId);
  const hasHistory = useMemo(
    () => flattenHistory(historyQuery.data).some(isExchange),
    [historyQuery.data],
  );

  const noteState = versionNoteState(availability);

  return (
    <section
      className="chat-panel"
      data-testid="chat-panel"
      data-availability={availability}
      aria-label="Ask about these reviews"
    >
      <HistoryPane datasetId={datasetId} />

      <SuggestionChips
        datasetId={datasetId}
        onSelect={setValue}
        hasHistory={hasHistory}
      />

      {noteState !== "none" && activeVersion != null && (
        <VersionNote activeVersion={activeVersion} state={noteState} />
      )}

      {/* StreamingExchange owns the pending/streaming row and the input; the
          panel controls the input value so a suggestion chip can prefill it
          (SuggestionChips.onSelect → setValue → this controlled value). */}
      <StreamingExchange
        datasetId={datasetId}
        availability={availability}
        value={value}
        onChange={setValue}
      />
    </section>
  );
}
