/**
 * Unit tests for `useRealtime` (dataset-library task 5, Requirements 6.1,
 * 6.3, 6.5, 6.6).
 *
 * Uses the shared WebSocket mock (`src/test/ws.ts`) and fake timers to assert:
 * - a single socket opens on mount;
 * - an incoming frame is applied to the cache (`check.updated` patch);
 * - an unexpected drop reconnects with exponential backoff (1 s → 2 s → … → 30 s cap);
 * - after a reconnect the hook refetches current state (resync).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { makeItem, makeSession } from "../test/fixtures";
import {
  installMockWebSocket,
  lastMockWebSocket,
  mockWebSockets,
  uninstallMockWebSocket,
  WS_OPEN,
} from "../test/ws";
import { backoffDelayMs, BASE_BACKOFF_MS, MAX_BACKOFF_MS, parseFrame, useRealtime } from "./useRealtime";
import { checkQueryKey } from "./useCheck";

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children);
  };
}

let client: QueryClient;

beforeEach(() => {
  vi.useFakeTimers();
  installMockWebSocket();
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});

afterEach(() => {
  uninstallMockWebSocket();
  vi.useRealTimers();
});

describe("backoffDelayMs (Req 6.6)", () => {
  it("doubles from 1 s and caps at 30 s", () => {
    expect(backoffDelayMs(0)).toBe(BASE_BACKOFF_MS); // 1 s
    expect(backoffDelayMs(1)).toBe(2_000);
    expect(backoffDelayMs(2)).toBe(4_000);
    expect(backoffDelayMs(3)).toBe(8_000);
    expect(backoffDelayMs(4)).toBe(16_000);
    expect(backoffDelayMs(5)).toBe(MAX_BACKOFF_MS); // 32 s → capped to 30 s
    expect(backoffDelayMs(10)).toBe(MAX_BACKOFF_MS);
  });
});

describe("parseFrame", () => {
  it("parses a dataset.status.changed frame", () => {
    const frame = parseFrame(JSON.stringify({ type: "dataset.status.changed", dataset_id: "ds-1" }));
    expect(frame?.type).toBe("dataset.status.changed");
  });

  it("parses a check.updated frame", () => {
    const frame = parseFrame({ type: "check.updated", check_id: "c" });
    expect(frame?.type).toBe("check.updated");
  });

  it("rejects unknown types and malformed JSON", () => {
    expect(parseFrame(JSON.stringify({ type: "something.else" }))).toBeNull();
    expect(parseFrame("{not json")).toBeNull();
    expect(parseFrame(null)).toBeNull();
  });
});

describe("useRealtime connection (Req 6.1)", () => {
  it("opens a single socket on mount", () => {
    renderHook(() => useRealtime({ wsUrl: "ws://test/realtime" }), { wrapper: wrapper(client) });
    expect(mockWebSockets).toHaveLength(1);
    expect(lastMockWebSocket()!.url).toBe("ws://test/realtime");
  });

  it("does not connect when disabled", () => {
    renderHook(() => useRealtime({ wsUrl: "ws://test/realtime", enabled: false }), {
      wrapper: wrapper(client),
    });
    expect(mockWebSockets).toHaveLength(0);
  });

  it("closes the socket on unmount and does not reconnect", () => {
    const { unmount } = renderHook(() => useRealtime({ wsUrl: "ws://test/realtime" }), {
      wrapper: wrapper(client),
    });
    const socket = lastMockWebSocket()!;
    socket.simulateOpen();
    unmount();
    // A close fired by unmount must not schedule a reconnect.
    vi.advanceTimersByTime(60_000);
    expect(mockWebSockets).toHaveLength(1);
  });
});

describe("useRealtime frame handling (Req 6.3, 6.5)", () => {
  it("applies an incoming check.updated frame to the cache", () => {
    client.setQueryData(
      checkQueryKey("check-1"),
      makeSession([makeItem({ item_id: "u1", state: "checking" })]),
    );
    renderHook(() => useRealtime({ wsUrl: "ws://test/realtime" }), { wrapper: wrapper(client) });

    const socket = lastMockWebSocket()!;
    socket.simulateOpen();
    socket.simulateMessage({ type: "check.updated", check_id: "check-1", item_id: "u1", state: "done", verdict: "will_work" });

    const session = client.getQueryData<ReturnType<typeof makeSession>>(checkQueryKey("check-1"))!;
    expect(session.items[0].state).toBe("done");
  });
});

describe("useRealtime reconnect + backoff (Req 6.6)", () => {
  it("reconnects after a drop using exponential backoff", () => {
    renderHook(() => useRealtime({ wsUrl: "ws://test/realtime" }), { wrapper: wrapper(client) });
    const first = lastMockWebSocket()!;
    first.simulateOpen();
    expect(mockWebSockets).toHaveLength(1);

    // Drop the connection; nothing reconnects before the 1 s backoff elapses.
    first.simulateDrop();
    vi.advanceTimersByTime(BASE_BACKOFF_MS - 1);
    expect(mockWebSockets).toHaveLength(1);

    // At 1 s the first reconnect fires.
    vi.advanceTimersByTime(1);
    expect(mockWebSockets).toHaveLength(2);

    // Second drop (reconnect attempt before open) backs off to 2 s.
    const second = lastMockWebSocket()!;
    second.simulateDrop();
    vi.advanceTimersByTime(2_000);
    expect(mockWebSockets).toHaveLength(3);
  });

  it("refetches current state after a successful reconnect (resync)", () => {
    const invalidate = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useRealtime({ wsUrl: "ws://test/realtime" }), { wrapper: wrapper(client) });

    const first = lastMockWebSocket()!;
    first.simulateOpen(); // first open → NOT a reconnect, no resync
    expect(invalidate).not.toHaveBeenCalled();

    first.simulateDrop();
    vi.advanceTimersByTime(BASE_BACKOFF_MS);
    const second = lastMockWebSocket()!;
    expect(second).not.toBe(first);

    second.simulateOpen(); // reconnect open → resync
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["datasets"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["check"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["dataset"] });
  });

  it("resets backoff to 1 s after a healthy reconnect", () => {
    renderHook(() => useRealtime({ wsUrl: "ws://test/realtime" }), { wrapper: wrapper(client) });
    const first = lastMockWebSocket()!;
    first.simulateOpen();

    first.simulateDrop();
    vi.advanceTimersByTime(BASE_BACKOFF_MS);
    const second = lastMockWebSocket()!;
    second.simulateOpen(); // healthy → attempt counter resets

    // Next drop should again wait only 1 s, not 2 s.
    second.simulateDrop();
    vi.advanceTimersByTime(BASE_BACKOFF_MS);
    expect(mockWebSockets).toHaveLength(3);
    expect(lastMockWebSocket()!.readyState).not.toBe(WS_OPEN); // fresh socket, not yet opened
  });
});
