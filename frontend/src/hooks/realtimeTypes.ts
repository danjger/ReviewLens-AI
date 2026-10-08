/**
 * Real-time frame types (dataset-library task 5).
 *
 * These mirror the client frames the backend push handler emits
 * (`backend/app/handlers/push.py` `_frame`): the EventBridge event detail with
 * a `type` key added — `{ ...detail, type }`. The two broadcast event types are
 * declared in `BROADCAST_DETAIL_TYPES` on the backend:
 *   - `dataset.status.changed` — detail from `app/db/status.py` `transition`
 *   - `check.updated`          — detail from `app/handlers/check.py`
 *
 * The frontend event bus switches on `type` and applies the rest of the frame
 * to the TanStack Query cache (see `realtimeReducers.ts`).
 *
 * A third broadcast frame, `chat.exchange.saved`, is added by guardrailed-chat
 * task 6.4: the chat/save services publish it after persisting an Exchange
 * (`app/chat/service.py` / `app/chat/save_api.py` `_EXCHANGE_SAVED_EVENT`), and
 * the push handler fans it out so every tab viewing that dataset adds the new
 * Exchange to its history without a reload (Requirement 5.7). Its body carries
 * IDs only (`dataset_id`, `exchange_id`, `key`), so the reducer refetches the
 * chat-history query rather than patching content it does not have.
 */

import type { VerdictLabel } from "../api/ingest";

/** `type` of a `dataset.status.changed` frame. */
export const DATASET_STATUS_CHANGED = "dataset.status.changed" as const;
/** `type` of a `check.updated` frame. */
export const CHECK_UPDATED = "check.updated" as const;
/** `type` of a `chat.exchange.saved` frame (guardrailed-chat task 6.4). */
export const CHAT_EXCHANGE_SAVED = "chat.exchange.saved" as const;

/**
 * A `dataset.status.changed` frame.
 *
 * Fields come from the status transition payload: `dataset_id`, `status`, the
 * event timestamp `at`, `data_version`, `active_version`, the human `message`,
 * and the dataset `metrics` map. `type` is added by the push handler.
 */
export interface DatasetStatusChangedFrame {
  type: typeof DATASET_STATUS_CHANGED;
  dataset_id: string;
  status: string;
  at: string;
  data_version: number | null;
  active_version: number | null;
  message: string;
  metrics: Record<string, unknown>;
}

/**
 * A `check.updated` frame.
 *
 * Fields come from the check handler: the `check_id`, the `item_id` whose
 * result changed, the item `state`, and the viability `verdict` label (null
 * when the item did not reach a verdict, e.g. an `error`). `type` is added by
 * the push handler.
 */
export interface CheckUpdatedFrame {
  type: typeof CHECK_UPDATED;
  check_id: string;
  item_id: string;
  state: string;
  verdict: VerdictLabel | null;
}

/**
 * A `chat.exchange.saved` frame (guardrailed-chat task 6.4, Requirement 5.7).
 *
 * Published by the chat/save services after an Exchange is persisted and fanned
 * out by the push handler. The body carries IDs only — `dataset_id`, the new
 * `exchange_id`, and the Exchange's S3 `key` (steering: "queue/event bodies
 * carry IDs, payloads live in S3/DB"). Because the content isn't in the frame,
 * the reducer refetches the dataset's chat-history query so the new Exchange
 * (another visitor's, or this tab's save-retry) appears without a reload.
 */
export interface ChatExchangeSavedFrame {
  type: typeof CHAT_EXCHANGE_SAVED;
  dataset_id: string;
  exchange_id: string;
  key: string;
}

/** Any frame the real-time channel can deliver. */
export type RealtimeFrame =
  | DatasetStatusChangedFrame
  | CheckUpdatedFrame
  | ChatExchangeSavedFrame;

/** A row as held in the `['datasets']` list cache (fields the push can patch). */
export interface DatasetRow {
  id: string;
  status?: string;
  display_state?: string;
  last_message?: string | null;
  review_count?: number | null;
  data_version?: number | null;
  active_version?: number | null;
  last_refreshed_at?: string | null;
  [key: string]: unknown;
}

/** Narrow an unknown frame to a `dataset.status.changed` frame. */
export function isDatasetStatusChanged(
  frame: RealtimeFrame,
): frame is DatasetStatusChangedFrame {
  return frame.type === DATASET_STATUS_CHANGED;
}

/** Narrow an unknown frame to a `check.updated` frame. */
export function isCheckUpdated(frame: RealtimeFrame): frame is CheckUpdatedFrame {
  return frame.type === CHECK_UPDATED;
}

/** Narrow an unknown frame to a `chat.exchange.saved` frame. */
export function isChatExchangeSaved(
  frame: RealtimeFrame,
): frame is ChatExchangeSavedFrame {
  return frame.type === CHAT_EXCHANGE_SAVED;
}
