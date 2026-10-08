/**
 * Polling-fallback intervals for the real-time channel (dataset-library task 5,
 * design "Error Handling": "If the WebSocket is unavailable, the frontend polls
 * /datasets every 10 seconds while any row is requested or processing, and
 * polls .../checks/{id} every 2 seconds while a Check is running").
 *
 * These are the backstop when the socket is down (and a safety net even when it
 * is up). They are exported as reusable constants and `refetchInterval` helpers
 * so the list query (task 6) and the existing Check query (`useCheck`) adopt the
 * same cadence instead of hard-coding their own.
 */

import type { CheckSession } from "../api/ingest";
import { isTerminal } from "../api/ingest";
import type { DatasetRow } from "./realtimeTypes";

/** Poll the Tracked Datasets list every 10 s while any row is in flight. */
export const LIST_POLL_INTERVAL_MS = 10_000;

/** Poll an active Check every 2 s while any item is still running. */
export const CHECK_POLL_INTERVAL_MS = 2_000;

/** Dataset statuses that mean work is still in flight (keep polling the list). */
const IN_FLIGHT_STATUSES: ReadonlySet<string> = new Set(["requested", "processing"]);

/**
 * `refetchInterval` for the `['datasets']` list query: poll every
 * {@link LIST_POLL_INTERVAL_MS} while any row is `requested`/`processing`, and
 * stop (return `false`) once every row has settled. Pass this straight to the
 * list `useQuery`.
 */
export function listRefetchInterval(rows: DatasetRow[] | undefined): number | false {
  if (!rows || rows.length === 0) return false;
  const anyInFlight = rows.some(
    (row) => typeof row.status === "string" && IN_FLIGHT_STATUSES.has(row.status),
  );
  return anyInFlight ? LIST_POLL_INTERVAL_MS : false;
}

/**
 * `refetchInterval` for the `['check', id]` query: poll every
 * {@link CHECK_POLL_INTERVAL_MS} while any item is non-terminal, else stop.
 * Mirrors the cadence `useCheck` should adopt as the real-time backstop.
 */
export function checkRefetchInterval(session: CheckSession | undefined): number | false {
  if (!session) return CHECK_POLL_INTERVAL_MS;
  const allDone = session.items.every(isTerminal);
  return allDone ? false : CHECK_POLL_INTERVAL_MS;
}
