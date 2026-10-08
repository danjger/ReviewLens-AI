/**
 * Typed client for the guardrailed-chat endpoints (guardrailed-chat task 6).
 *
 * This is the shared chat frontend foundation that the 6.x subtasks build on.
 * Task 6.1 needs only the shared **history** timeline endpoint:
 *
 *   GET /datasets/{id}/chat/history?before=&limit=20
 *
 * which the backend serves from `app/chat/history_api.py` as the merged,
 * oldest-first timeline of Exchanges and refresh markers
 * (`app/chat/history.py`), returning `{ items, next_before, has_more }`
 * (design "Endpoints"; Requirements 5.2, 5.3, 5.4).
 *
 * Later subtasks extend this module: the streaming `POST /api/chat/datasets/{id}`
 * SSE client (6.4), the `POST /datasets/{id}/chat/save` retry (6.4), and the
 * `GET /datasets/{id}/chat/suggestions` list (6.5). They are intentionally not
 * built here (task 6.1 scope), but the timeline types below are shared so those
 * tasks reuse them.
 *
 * Error handling reuses {@link ApiError} from `./ingest`, so a non-2xx response
 * carries the backend `{ error: { code, message } }` envelope (and `429`
 * `Retry-After`) consistently with the Check/Dataset clients.
 */

import { ApiError } from "./ingest";

/** The scope result stored on an Exchange (backend Exchange `scope`). */
export type ChatScope = "in_scope" | "declined";

/**
 * One cited review's saved snippet (design `citation_snippets` value;
 * backend `postprocess.CitationSnippet`).
 *
 * The popover reads these saved fields rather than re-looking the ID up, so a
 * citation keeps showing the right review after the data is refreshed
 * (Requirement 2.2). Review IDs are only unique within one data version.
 */
export interface CitationSnippet {
  /** The cited review's text (copied from the source page; never generated). */
  text: string;
  /** The review's star rating, or null when it had none. */
  rating: number | null;
  /** The review's date as stored, or null when unknown. */
  date: string | null;
}

/**
 * A saved Exchange as returned in the history timeline
 * (backend Exchange "Data Models" shape plus the history layer's `type` and
 * `is_stale`). This is one question/answer with its evidence and metadata.
 *
 * `is_stale` is added by the history layer: the Exchange's `data_version` is
 * older than the dataset's `active_version` (Requirement 9.4). The citation
 * snippets travel with the Exchange so a stale Exchange's popovers still work.
 */
export interface ChatExchange {
  /** Timeline discriminator — always `"exchange"` for an Exchange row. */
  type: "exchange";
  /** The Exchange's stable id (uuid). */
  id: string;
  /** The dataset this Exchange belongs to. */
  dataset_id: string;
  /** The data version the answer was grounded in. */
  data_version: number;
  /** The asking browser tab's random conversation id (never shown). */
  conversation_id: string;
  /** ISO-8601 time the question was asked (the timeline cursor). */
  asked_at: string;
  /** ISO-8601 time the answer finished, when present. */
  answered_at?: string | null;
  /** The analyst's question text. */
  question: string;
  /** The assistant's answer text (declines included). */
  answer: string;
  /** The surviving citation IDs, in first-seen order. */
  citations: string[];
  /** How many invalid citations were dropped before saving. */
  dropped_citations: number;
  /** Saved snippet for each surviving citation, keyed by review id. */
  citation_snippets: Record<string, CitationSnippet>;
  /** Whether the answer was in scope or a decline (Requirement 3.6). */
  scope: ChatScope;
  /** The decline category when `scope === "declined"`, else null. */
  scope_category?: string | null;
  /**
   * True when this Exchange's `data_version` is older than the dataset's
   * `active_version` — the reviews have changed since (Requirement 9.4). Added
   * by the history layer, so it is always present on a timeline Exchange.
   */
  is_stale: boolean;
}

/** The three marker states (backend `history.MARKER_*`). */
export type RefreshMarkerState = "completed" | "pending" | "failed";

/**
 * A refresh marker row in the timeline (design "Refresh markers"; backend
 * `history.build_markers`). One is emitted per data version after the first.
 *
 * Task 6.1 renders a lightweight placeholder for this row; the full
 * `RefreshMarker` UI (date/time, trigger wording, before/after counts, pending
 * spinner, failed state) is task 6.6. The fields are typed here so 6.6 slots in.
 */
export interface RefreshMarker {
  /** Timeline discriminator — always `"refresh_marker"` for a marker row. */
  type: "refresh_marker";
  /** The data version this marker announces (always > 1). */
  version: number;
  /** Completed, pending (still refreshing), or failed. */
  state: RefreshMarkerState;
  /** What started the refresh (manual, duplicate submission, upload replace). */
  trigger: string;
  /** ISO-8601 time the refresh was requested (pending marker position). */
  requested_at: string | null;
  /** ISO-8601 completion time, or null while pending (terminal position). */
  completed_at: string | null;
  /** This version's review count, or null until it completes. */
  review_count: number | null;
  /** The prior version's review count ("was 180"), or null when unknown. */
  previous_review_count: number | null;
}

/** One row of the merged timeline: an Exchange or a refresh marker. */
export type TimelineItem = ChatExchange | RefreshMarker;

/** True when a timeline item is an Exchange row (narrowing helper). */
export function isExchange(item: TimelineItem): item is ChatExchange {
  return item.type === "exchange";
}

/** True when a timeline item is a refresh marker row (narrowing helper). */
export function isRefreshMarker(item: TimelineItem): item is RefreshMarker {
  return item.type === "refresh_marker";
}

/**
 * One page of the shared history timeline, oldest first
 * (`GET /datasets/{id}/chat/history`; backend `history.HistoryPage.to_dict`).
 *
 * - `items`: the page's timeline rows, oldest first (Requirement 5.2).
 * - `next_before`: the cursor to pass as `before` to fetch the immediately
 *   older page, or null when this page is the start of the timeline.
 * - `has_more`: true when older items exist beyond this page.
 */
export interface HistoryPage {
  items: TimelineItem[];
  next_before: string | null;
  has_more: boolean;
}

/** The shared history page size (design / Requirement 5.3). */
export const HISTORY_PAGE_SIZE = 20;

const BASE = "/api";

/** Resolve an API path to a same-origin absolute URL (mirrors `./ingest`). */
function url(path: string): string {
  const origin =
    typeof window !== "undefined" && window.location?.origin
      ? window.location.origin
      : "http://localhost";
  return `${origin}${BASE}${path}`;
}

/** Parse a `Retry-After` header value (seconds) into a number, or null. */
function parseRetryAfter(response: Response): number | null {
  const raw = response.headers.get("Retry-After");
  if (raw == null) return null;
  const seconds = Number.parseInt(raw, 10);
  return Number.isFinite(seconds) ? seconds : null;
}

/** Throw an {@link ApiError} describing a non-ok response. */
async function throwApiError(response: Response): Promise<never> {
  let code = "UNKNOWN";
  let message = response.statusText || `Request failed (${response.status})`;
  try {
    const body = (await response.json()) as {
      error?: { code?: string; message?: string };
    };
    if (body.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
    }
  } catch {
    // Non-JSON body; keep the status-line fallback.
  }
  throw new ApiError(response.status, code, message, parseRetryAfter(response));
}

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    await throwApiError(response);
  }
  return (await response.json()) as T;
}

/**
 * Fetch a dataset's 3–4 suggested starter questions
 * (`GET /datasets/{id}/chat/suggestions`; guardrailed-chat task 6.5,
 * Requirement 1.4).
 *
 * The backend builds these from the active version's theme labels using
 * templates, falling back to general questions that fit any review set when no
 * themes are available (`app/chat/suggestions_api.py`; **no AI call**). The
 * response envelope is `{ suggestions: string[] }`; this returns the bare
 * `string[]` so callers (the {@link SuggestionChips} hook) render chips without
 * unwrapping. An unknown dataset id yields the general fallback list, so the
 * array is never empty — but callers still tolerate an empty array defensively.
 *
 * The component does not distinguish theme-based from fallback questions: it
 * just renders whatever strings the backend returns.
 */
export async function getChatSuggestions(id: string): Promise<string[]> {
  const response = await fetch(
    url(`/datasets/${encodeURIComponent(id)}/chat/suggestions`),
  );
  const body = await parseJson<{ suggestions?: string[] }>(response);
  return body.suggestions ?? [];
}

/** Params for {@link getChatHistory}. */
export interface GetChatHistoryParams {
  /**
   * The backward-paging cursor returned as `next_before` from the previous
   * (newer) page; omit (or null) to fetch the newest page — the end of the
   * timeline (Requirement 5.3).
   */
  before?: string | null;
  /** Page size (default {@link HISTORY_PAGE_SIZE}, i.e. 20). */
  limit?: number;
}

/**
 * Fetch one page of a dataset's shared Q&A history timeline
 * (`GET /datasets/{id}/chat/history?before=&limit=20`).
 *
 * Returns the newest page when `before` is omitted, or the page immediately
 * older than `before` otherwise. The page is oldest-first; its `next_before`
 * cursor fetches the next older page. An unknown dataset id yields an empty
 * page (the no-sign-in app never leaks whether an id exists).
 */
export async function getChatHistory(
  id: string,
  params: GetChatHistoryParams = {},
): Promise<HistoryPage> {
  const search = new URLSearchParams();
  if (params.before != null && params.before.length > 0) {
    search.set("before", params.before);
  }
  search.set("limit", String(params.limit ?? HISTORY_PAGE_SIZE));
  const response = await fetch(
    url(`/datasets/${encodeURIComponent(id)}/chat/history?${search.toString()}`),
  );
  return parseJson<HistoryPage>(response);
}
// ── Streaming chat (guardrailed-chat task 6.4) ──────────────────────────────

/**
 * The `done` SSE payload: the fully assembled Exchange the chat service streams
 * when the answer finishes (`app/chat/service.py` `_build_done_payload` /
 * `_persist_exchange`). It is the saved Exchange "Data Models" shape plus two
 * streaming-only fields:
 *
 * - `saved`: `false` when the first S3 save failed, so the UI shows a warning
 *   and a Retry button (Requirement 5.5, design "Error Handling").
 * - `signature`: an HMAC over the Exchange content (excluding `saved` and
 *   `signature` themselves), so the retry-save endpoint can verify the exact
 *   payload the service produced and refuse forgeries (Correctness Property 4).
 *
 * It intentionally does NOT carry the history layer's `type`/`is_stale` fields
 * (those are added when the Exchange is later read back through the timeline).
 * {@link doneToExchange} adapts a `done` payload into a {@link ChatExchange}
 * timeline row so the merge-on-done can reuse the Exchange row component.
 */
export interface SavedExchange {
  id: string;
  dataset_id: string;
  data_version: number;
  conversation_id: string;
  asked_at: string;
  answered_at?: string | null;
  question: string;
  answer: string;
  citations: string[];
  dropped_citations: number;
  citation_snippets: Record<string, CitationSnippet>;
  scope: ChatScope;
  scope_category?: string | null;
  /** Whether the Exchange was persisted; `false` means the UI must offer Retry. */
  saved: boolean;
  /** The server HMAC over the Exchange content; replayed to the save endpoint. */
  signature: string;
  [key: string]: unknown;
}

/**
 * Adapt a `done` {@link SavedExchange} payload into a {@link ChatExchange}
 * timeline row so a freshly answered Exchange can be merged into the history
 * cache and rendered by the same Exchange row (merge-on-done, Requirement 6.3).
 *
 * A just-answered Exchange is always on the current data version, so it is
 * never stale (`is_stale: false`); the `type` discriminator is added. The
 * streaming-only `saved`/`signature` fields are dropped — they are not part of
 * a timeline row and `is_stale` + `type` are what the pane keys off.
 */
export function doneToExchange(payload: SavedExchange): ChatExchange {
  return {
    type: "exchange",
    id: payload.id,
    dataset_id: payload.dataset_id,
    data_version: payload.data_version,
    conversation_id: payload.conversation_id,
    asked_at: payload.asked_at,
    answered_at: payload.answered_at ?? null,
    question: payload.question,
    answer: payload.answer,
    citations: payload.citations,
    dropped_citations: payload.dropped_citations,
    citation_snippets: payload.citation_snippets,
    scope: payload.scope,
    scope_category: payload.scope_category ?? null,
    is_stale: false,
  };
}

/**
 * The three SSE events the chat stream emits (design "Endpoints"; backend
 * `app/chat/sse.py`):
 *
 * - `token`: one chunk of the answer, `{ text }` — appended to the live answer.
 * - `done`: the terminal {@link SavedExchange} payload.
 * - `error`: a mid-stream failure, `{ code, message }` — the answer was
 *   interrupted and nothing was saved (design "Error Handling").
 */
export type ChatStreamEvent =
  | { type: "token"; text: string }
  | { type: "done"; exchange: SavedExchange }
  | { type: "error"; code: string; message: string };

/** Callbacks a {@link streamChat} caller supplies for each stream event. */
export interface StreamChatHandlers {
  /** Called for every `token` event, with the chunk to append to the answer. */
  onToken: (text: string) => void;
  /** Called once with the final Exchange when the `done` event arrives. */
  onDone: (exchange: SavedExchange) => void;
  /** Called on a mid-stream `error` event (the answer was interrupted). */
  onError: (code: string, message: string) => void;
}

/** Body of a {@link streamChat} request. */
export interface StreamChatRequest {
  /** The analyst's question (already trimmed and length-validated). */
  question: string;
  /** The asking tab's conversation id (from {@link getConversationId}). */
  conversation_id: string;
}

/**
 * Parse the SSE events out of a growing buffer, returning the parsed events and
 * the unconsumed tail (a partial event not yet terminated by a blank line).
 *
 * SSE frames are separated by a blank line (`\n\n`); each frame has an
 * `event:` line and a `data:` line (`app/chat/sse.py` `encode_event`). Only
 * these two fields are produced by the backend, so the parser reads the event
 * name and the single-line JSON `data` payload. Unknown event names are
 * skipped. Exported for unit testing without a network stream.
 */
export function parseSseBuffer(buffer: string): {
  events: ChatStreamEvent[];
  rest: string;
} {
  const events: ChatStreamEvent[] = [];
  // Normalise CRLF so the split on a blank line works regardless of newline.
  const normalised = buffer.replace(/\r\n/g, "\n");
  const parts = normalised.split("\n\n");
  // The last element is the (possibly empty) unterminated remainder.
  const rest = parts.pop() ?? "";

  for (const frame of parts) {
    if (frame.trim().length === 0) continue;
    let eventName = "message";
    const dataLines: string[] = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) {
        eventName = line.slice("event:".length).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice("data:".length).replace(/^ /, ""));
      }
    }
    if (dataLines.length === 0) continue;
    let data: unknown;
    try {
      data = JSON.parse(dataLines.join("\n"));
    } catch {
      // A malformed data payload is skipped rather than aborting the stream.
      continue;
    }
    const parsed = toStreamEvent(eventName, data);
    if (parsed) events.push(parsed);
  }

  return { events, rest };
}

/** Build a typed {@link ChatStreamEvent} from a raw event name + data, or null. */
function toStreamEvent(eventName: string, data: unknown): ChatStreamEvent | null {
  if (data == null || typeof data !== "object") return null;
  const obj = data as Record<string, unknown>;
  if (eventName === "token") {
    return { type: "token", text: typeof obj.text === "string" ? obj.text : "" };
  }
  if (eventName === "done") {
    return { type: "done", exchange: obj as unknown as SavedExchange };
  }
  if (eventName === "error") {
    return {
      type: "error",
      code: typeof obj.code === "string" ? obj.code : "CHAT_STREAM_ERROR",
      message:
        typeof obj.message === "string" ? obj.message : "Answer interrupted — retry",
    };
  }
  return null;
}

/**
 * Open a streaming chat request and dispatch each SSE event to the handlers
 * (`POST /api/chat/datasets/{id}` with `{question, conversation_id}`; design
 * "Endpoints" / "Architecture").
 *
 * `EventSource` can only issue GET requests, so the stream is read with
 * `fetch` + a `ReadableStream` reader: the response body is decoded
 * incrementally, {@link parseSseBuffer} pulls complete SSE frames out of the
 * growing buffer, and each frame is handed to the matching handler. `token`
 * events append to the live answer, `done` carries the final Exchange, and an
 * `error` event means the answer was interrupted (design "Error Handling").
 *
 * A non-2xx response is thrown as an {@link ApiError} before any streaming,
 * so the caller can surface a `429` to the input's rate-limit state and a
 * `409` (`CHAT_UNAVAILABLE`) as an unavailable dataset.
 *
 * @param signal - optional abort signal to cancel an in-flight stream.
 */
export async function streamChat(
  id: string,
  body: StreamChatRequest,
  handlers: StreamChatHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(url(`/chat/datasets/${encodeURIComponent(id)}`), {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok) {
    await throwApiError(response);
  }
  if (response.body == null) {
    // No stream to read (e.g. a stubbed empty body): nothing to dispatch.
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const { events, rest } = parseSseBuffer(buffer);
    buffer = rest;
    for (const event of events) dispatchStreamEvent(event, handlers);
  }

  // Flush any complete frame left in the buffer after the stream closes.
  buffer += decoder.decode();
  const { events } = parseSseBuffer(buffer.endsWith("\n\n") ? buffer : `${buffer}\n\n`);
  for (const event of events) dispatchStreamEvent(event, handlers);
}

/** Route one parsed stream event to the matching handler. */
function dispatchStreamEvent(event: ChatStreamEvent, handlers: StreamChatHandlers): void {
  if (event.type === "token") handlers.onToken(event.text);
  else if (event.type === "done") handlers.onDone(event.exchange);
  else handlers.onError(event.code, event.message);
}

/**
 * Retry persisting an Exchange whose first save failed
 * (`POST /datasets/{id}/chat/save`; Requirement 5.5, design "Endpoints").
 *
 * The body is the exact `done` {@link SavedExchange} payload, including its
 * HMAC `signature`: the endpoint recomputes the signature and refuses (403)
 * anything that doesn't verify, so a visitor can't write a forged Exchange into
 * the shared history (Correctness Property 4). On success the Exchange is
 * persisted and `chat.exchange.saved` is published, so every tab (including
 * this one, via the realtime refetch) sees it in the timeline.
 */
export async function postChatSave(
  id: string,
  exchange: SavedExchange,
): Promise<void> {
  const response = await fetch(
    url(`/datasets/${encodeURIComponent(id)}/chat/save`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(exchange),
    },
  );
  if (!response.ok) {
    await throwApiError(response);
  }
}
