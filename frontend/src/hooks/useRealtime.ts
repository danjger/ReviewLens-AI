/**
 * useRealtime — the Portal's single real-time WebSocket per tab
 * (dataset-library task 5, Requirements 6.1, 6.3, 6.4, 6.5, 6.6).
 *
 * Responsibilities (design "Frontend"):
 * - Open ONE WebSocket for the tab and keep it open for the lifetime of the
 *   mount (the hook is meant to be mounted once, near the app root).
 * - On every incoming frame, parse it and hand it to the cache reducers
 *   (`applyRealtimeFrame`), which patch the list, detail, and check caches in
 *   place (Requirements 6.3–6.5).
 * - On an unexpected disconnect, reconnect with exponential backoff from 1 s up
 *   to a 30 s cap (Requirement 6.6).
 * - After a reconnect, invalidate the server-state queries so the tab refetches
 *   current state and recovers any events missed while offline (Requirement
 *   6.6: "refetch current state after reconnecting").
 *
 * The socket URL comes from config (a `wsUrl` option, else `VITE_WS_URL`, else
 * a same-origin `/realtime` default), never a literal baked into component
 * code. The connect handshake needs no credentials (design: the WS API stage
 * throttles instead).
 *
 * Backoff is computed by the pure {@link backoffDelayMs} so it can be unit
 * tested without timers.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { applyRealtimeFrame, DATASETS_QUERY_KEY } from "./realtimeReducers";
import type { RealtimeFrame } from "./realtimeTypes";
import {
  CHAT_EXCHANGE_SAVED,
  CHECK_UPDATED,
  DATASET_STATUS_CHANGED,
} from "./realtimeTypes";

/** First reconnect delay (ms). Doubles each attempt up to {@link MAX_BACKOFF_MS}. */
export const BASE_BACKOFF_MS = 1_000;

/** Reconnect backoff cap (ms). */
export const MAX_BACKOFF_MS = 30_000;

/**
 * Exponential backoff delay for a reconnect attempt.
 *
 * Attempt 0 (the first reconnect) waits {@link BASE_BACKOFF_MS}; each later
 * attempt doubles, capped at {@link MAX_BACKOFF_MS}:
 *   attempt 0 → 1s, 1 → 2s, 2 → 4s, 3 → 8s, 4 → 16s, 5+ → 30s.
 *
 * @param attempt - zero-based count of reconnects already tried.
 */
export function backoffDelayMs(attempt: number): number {
  const exp = BASE_BACKOFF_MS * 2 ** Math.max(0, attempt);
  return Math.min(exp, MAX_BACKOFF_MS);
}

/** Resolve the WebSocket URL from an explicit option, env, or a default. */
function resolveWsUrl(explicit?: string): string {
  if (explicit) return explicit;
  const fromEnv =
    typeof import.meta !== "undefined"
      ? (import.meta as { env?: Record<string, string | undefined> }).env?.VITE_WS_URL
      : undefined;
  if (fromEnv) return fromEnv;
  if (typeof window !== "undefined" && window.location) {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${window.location.host}/realtime`;
  }
  return "ws://localhost/realtime";
}

/** Parse a raw message payload into a typed frame, or null if unusable. */
export function parseFrame(data: unknown): RealtimeFrame | null {
  let obj: unknown = data;
  if (typeof data === "string") {
    try {
      obj = JSON.parse(data);
    } catch {
      return null;
    }
  }
  if (obj == null || typeof obj !== "object") return null;
  const type = (obj as { type?: unknown }).type;
  if (
    type === DATASET_STATUS_CHANGED ||
    type === CHECK_UPDATED ||
    type === CHAT_EXCHANGE_SAVED
  ) {
    return obj as RealtimeFrame;
  }
  return null;
}

export interface UseRealtimeOptions {
  /** Override the socket URL (defaults to `VITE_WS_URL` or same-origin). */
  wsUrl?: string;
  /** Set false to disable the connection (e.g. in a test that doesn't need it). */
  enabled?: boolean;
}

/**
 * Open and maintain the tab's real-time channel. Mount once near the app root.
 *
 * Returns nothing: all effects land in the TanStack Query cache, which the
 * Library and detail page already read.
 */
export function useRealtime(options: UseRealtimeOptions = {}): void {
  const { wsUrl, enabled = true } = options;
  const queryClient = useQueryClient();

  // Keep mutable connection state in refs so the single effect below owns the
  // socket for the whole mount (no reconnect churn from re-renders).
  const socketRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hasConnectedRef = useRef(false);
  const closedByUnmountRef = useRef(false);

  useEffect(() => {
    if (!enabled) return;
    closedByUnmountRef.current = false;
    const url = resolveWsUrl(wsUrl);

    function clearTimer(): void {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    }

    function scheduleReconnect(): void {
      if (closedByUnmountRef.current) return;
      const delay = backoffDelayMs(attemptRef.current);
      attemptRef.current += 1;
      clearTimer();
      timerRef.current = setTimeout(connect, delay);
    }

    /** Refetch server state so events missed while offline are recovered. */
    function resync(): void {
      void queryClient.invalidateQueries({ queryKey: [...DATASETS_QUERY_KEY] });
      void queryClient.invalidateQueries({ queryKey: ["check"] });
      void queryClient.invalidateQueries({ queryKey: ["dataset"] });
    }

    function connect(): void {
      if (closedByUnmountRef.current) return;
      const socket = new WebSocket(url);
      socketRef.current = socket;

      socket.onopen = () => {
        const reconnected = hasConnectedRef.current;
        hasConnectedRef.current = true;
        attemptRef.current = 0;
        // After a reconnect (not the first connect), resync missed state.
        if (reconnected) resync();
      };

      socket.onmessage = (event: MessageEvent) => {
        const frame = parseFrame(event.data);
        if (frame) applyRealtimeFrame(queryClient, frame);
      };

      socket.onerror = () => {
        // `close` follows an error; reconnect is scheduled there.
      };

      socket.onclose = () => {
        socketRef.current = null;
        if (!closedByUnmountRef.current) scheduleReconnect();
      };
    }

    connect();

    return () => {
      closedByUnmountRef.current = true;
      clearTimer();
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        try {
          socket.close();
        } catch {
          // ignore — closing an already-closed socket is harmless.
        }
      }
    };
    // Reconnect only if the URL or enabled flag changes; queryClient is stable.
  }, [wsUrl, enabled, queryClient]);
}
