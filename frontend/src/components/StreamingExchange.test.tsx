/**
 * Tests for {@link StreamingExchange} and {@link useStreamingExchange}
 * (guardrailed-chat task 6.4, Requirements 5.5, 6.2, 6.3, 7.1).
 *
 * Focused coverage of the streaming lifecycle (comprehensive ChatPanel tests
 * are task 6.9):
 *
 * - tokens render incrementally into the live answer, then the Exchange merges
 *   into history on `done` and the pending row clears (Requirements 6.2, 6.3);
 * - a mid-stream `error` event shows the interrupted alert + Retry;
 * - a `done` with `saved:false` shows the unsaved warning + Retry; Retry calls
 *   the save endpoint and flips to saved (Requirement 5.5);
 * - a `chat.exchange.saved` realtime frame for this dataset refetches the
 *   history so the new Exchange appears (Requirement 5.7);
 * - the `conversation_id` persists in `sessionStorage` across stream calls.
 *
 * The SSE endpoint is mocked with MSW by streaming a `ReadableStream` body of
 * `text/event-stream` frames; the live frame uses the MockWebSocket helper.
 * Selectors are data-testid / data-* only; no assertions on counts/dates.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { type SavedExchange } from "../api/chat";
import { chatHistoryQueryKey, flattenHistory } from "../hooks/useChatHistory";
import { getConversationId } from "../hooks/conversationId";
import { applyRealtimeFrame } from "../hooks/realtimeReducers";
import { makeChatExchange, makeHistoryPage } from "../test/fixtures";
import { server } from "../test/server";
import HistoryPane from "./HistoryPane";
import StreamingExchange from "./StreamingExchange";

/** Build a done SSE Exchange payload (streaming shape: + saved + signature). */
function makeDonePayload(overrides: Partial<SavedExchange> = {}): SavedExchange {
  const base = makeChatExchange({ id: "ex-new", question: "Q?", answer: "Final answer [r_0001]." });
  // Strip the timeline-only fields; add the streaming-only ones.
  const { type, is_stale, ...rest } = base;
  void type;
  void is_stale;
  return {
    ...rest,
    saved: true,
    signature: "deadbeef",
    ...overrides,
  };
}

/** Encode SSE frames (mirrors backend `app/chat/sse.py`). */
function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/**
 * An MSW handler for the streaming chat endpoint that writes the given SSE
 * chunks to a ReadableStream body with a short delay between them, so the
 * client parses them incrementally.
 */
function streamingChatHandler(id: string, chunks: string[], status = 200) {
  return http.post(`/api/chat/datasets/${id}`, () => {
    const stream = new ReadableStream({
      async start(controller) {
        const encoder = new TextEncoder();
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
          // Yield to the event loop so the reader observes partial progress.
          await new Promise((r) => setTimeout(r, 1));
        }
        controller.close();
      },
    });
    return new HttpResponse(stream, {
      status,
      headers: { "Content-Type": "text/event-stream" },
    });
  });
}

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderPanel(client: QueryClient, datasetId = "ds-1") {
  return render(
    <QueryClientProvider client={client}>
      <HistoryPane datasetId={datasetId} />
      <StreamingExchange datasetId={datasetId} availability="available" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  try {
    window.sessionStorage.clear();
  } catch {
    /* ignore */
  }
});

afterEach(() => {
  server.resetHandlers();
});

describe("StreamingExchange — streaming + merge on done (Req 6.2, 6.3)", () => {
  it("renders tokens incrementally then merges the Exchange into history", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    // A deferred stream: the token frames flush immediately, but the `done`
    // frame is withheld until the test releases it, so the intermediate
    // streaming render is observable before completion (Requirement 6.2).
    let releaseDone: () => void = () => {};
    const donePromise = new Promise<void>((resolve) => {
      releaseDone = resolve;
    });
    server.use(
      http.get("/api/datasets/ds-1/chat/history", () =>
        HttpResponse.json(makeHistoryPage([])),
      ),
      http.post("/api/chat/datasets/ds-1", () => {
        const stream = new ReadableStream({
          async start(controller) {
            const enc = new TextEncoder();
            controller.enqueue(enc.encode(sseFrame("token", { text: "Hello " })));
            controller.enqueue(enc.encode(sseFrame("token", { text: "world" })));
            await donePromise;
            controller.enqueue(
              enc.encode(sseFrame("done", makeDonePayload({ id: "ex-new", answer: "Hello world" }))),
            );
            controller.close();
          },
        });
        return new HttpResponse(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }),
    );

    renderPanel(client);
    await screen.findByTestId("history-pane");

    await user.type(screen.getByTestId("chat-input-textarea"), "What's up?");
    await user.keyboard("{Enter}");

    // The pending question + streaming answer appear while generating (6.2).
    await screen.findByTestId("streaming-question");
    await waitFor(() =>
      expect(screen.getByTestId("streaming-answer").textContent).toContain("Hello"),
    );
    expect(screen.getByTestId("streaming-exchange")).toHaveAttribute("data-phase", "streaming");

    // Release the done frame; the pending row clears and the Exchange is in
    // history (6.3).
    releaseDone();
    await waitFor(() =>
      expect(screen.getByTestId("streaming-exchange")).toHaveAttribute("data-phase", "idle"),
    );
    const merged = flattenHistory(
      client.getQueryData(chatHistoryQueryKey("ds-1")),
    );
    expect(merged.some((item) => item.type === "exchange" && item.id === "ex-new")).toBe(true);
  });
});

describe("StreamingExchange — interrupted (design 'Error Handling')", () => {
  it("shows the interrupted alert + Retry on a mid-stream error", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    server.use(
      http.get("/api/datasets/ds-1/chat/history", () =>
        HttpResponse.json(makeHistoryPage([])),
      ),
      streamingChatHandler("ds-1", [
        sseFrame("token", { text: "Partial" }),
        sseFrame("error", { code: "CHAT_STREAM_ERROR", message: "Answer interrupted — retry" }),
      ]),
    );

    renderPanel(client);
    await screen.findByTestId("history-pane");

    await user.type(screen.getByTestId("chat-input-textarea"), "Why?");
    await user.keyboard("{Enter}");

    await screen.findByTestId("streaming-interrupted");
    expect(screen.getByTestId("streaming-exchange")).toHaveAttribute("data-phase", "interrupted");
    // The partial answer is kept so the analyst sees what was produced.
    expect(screen.getByTestId("streaming-answer").textContent).toContain("Partial");
    expect(screen.getByTestId("streaming-interrupted-retry")).toBeInTheDocument();
  });
});

describe("StreamingExchange — unsaved warning + Retry (Req 5.5)", () => {
  it("shows the unsaved warning and Retry calls save then flips to saved", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    let savedBody: Record<string, unknown> | null = null;
    server.use(
      http.get("/api/datasets/ds-1/chat/history", () =>
        HttpResponse.json(makeHistoryPage([])),
      ),
      streamingChatHandler("ds-1", [
        sseFrame("token", { text: "Answer" }),
        sseFrame("done", makeDonePayload({ id: "ex-unsaved", answer: "Answer", saved: false })),
      ]),
      http.post("/api/datasets/ds-1/chat/save", async ({ request }) => {
        savedBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ok: true }, { status: 200 });
      }),
    );

    renderPanel(client);
    await screen.findByTestId("history-pane");

    await user.type(screen.getByTestId("chat-input-textarea"), "Ask");
    await user.keyboard("{Enter}");

    const unsaved = await screen.findByTestId("streaming-unsaved");
    expect(unsaved).toBeInTheDocument();

    await user.click(screen.getByTestId("streaming-unsaved-retry"));

    // The save endpoint received the signed Exchange and the UI flips to saved.
    await waitFor(() =>
      expect(screen.getByTestId("streaming-exchange")).toHaveAttribute("data-phase", "idle"),
    );
    const captured = savedBody as Record<string, unknown> | null;
    expect(captured).not.toBeNull();
    expect(captured?.signature).toBe("deadbeef");
    const merged = flattenHistory(client.getQueryData(chatHistoryQueryKey("ds-1")));
    expect(merged.some((item) => item.type === "exchange" && item.id === "ex-unsaved")).toBe(true);
  });
});

describe("StreamingExchange — rate limit surfaced to the input (Error Handling)", () => {
  it("surfaces a 429 to the chat input and drops the pending row", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    server.use(
      http.get("/api/datasets/ds-1/chat/history", () =>
        HttpResponse.json(makeHistoryPage([])),
      ),
      http.post("/api/chat/datasets/ds-1", () =>
        HttpResponse.json(
          { error: { code: "RATE_LIMITED", message: "Too many questions." } },
          { status: 429, headers: { "Retry-After": "30" } },
        ),
      ),
    );

    renderPanel(client);
    await screen.findByTestId("history-pane");

    await user.type(screen.getByTestId("chat-input-textarea"), "Ask");
    await user.keyboard("{Enter}");

    await screen.findByTestId("chat-input-rate-limit");
    expect(screen.getByTestId("streaming-exchange")).toHaveAttribute("data-phase", "idle");
  });
});

describe("chat.exchange.saved refetch (Req 5.7)", () => {
  it("refetches history for this dataset when a chat.exchange.saved frame lands", async () => {
    const client = makeClient();
    // First history fetch is empty; after the frame, the refetch returns the
    // other visitor's new Exchange.
    let call = 0;
    server.use(
      http.get("/api/datasets/ds-1/chat/history", () => {
        call += 1;
        if (call === 1) return HttpResponse.json(makeHistoryPage([]));
        return HttpResponse.json(
          makeHistoryPage([makeChatExchange({ id: "ex-other", question: "Other tab?" })]),
        );
      }),
    );

    render(
      <QueryClientProvider client={client}>
        <HistoryPane datasetId="ds-1" />
      </QueryClientProvider>,
    );
    // Wait for the first (empty) load.
    await waitFor(() => expect(call).toBe(1));

    // Simulate the realtime frame → reducer invalidates the chat-history query.
    applyRealtimeFrame(client, {
      type: "chat.exchange.saved",
      dataset_id: "ds-1",
      exchange_id: "ex-other",
      key: "datasets/ds-1/chat/2026-ex-other.json",
    });

    await waitFor(() => {
      const items = flattenHistory(client.getQueryData(chatHistoryQueryKey("ds-1")));
      expect(items.some((item) => item.type === "exchange" && item.id === "ex-other")).toBe(true);
    });
  });

  it("ignores a chat.exchange.saved frame for a different dataset", async () => {
    const client = makeClient();
    client.setQueryData(chatHistoryQueryKey("ds-1"), {
      pages: [makeHistoryPage([makeChatExchange({ id: "ex-1" })])],
      pageParams: [null],
    });
    const before = client.getQueryState(chatHistoryQueryKey("ds-1"))?.dataUpdateCount ?? 0;

    applyRealtimeFrame(client, {
      type: "chat.exchange.saved",
      dataset_id: "ds-OTHER",
      exchange_id: "ex-x",
      key: "k",
    });

    // ds-1's cache is untouched (no refetch triggered for a different dataset).
    const after = client.getQueryState(chatHistoryQueryKey("ds-1"))?.dataUpdateCount ?? 0;
    expect(after).toBe(before);
  });
});

describe("conversation_id persistence (Req 2.6)", () => {
  it("persists the conversation id in sessionStorage across calls", () => {
    const first = getConversationId();
    const second = getConversationId();
    expect(first).toBe(second);
    expect(window.sessionStorage.getItem("reviewlens.chat.conversation_id")).toBe(first);
  });

  it("is reused by a stream request body", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    const stored = getConversationId();
    let sentConversationId: string | null = null;
    server.use(
      http.get("/api/datasets/ds-1/chat/history", () =>
        HttpResponse.json(makeHistoryPage([])),
      ),
      http.post("/api/chat/datasets/ds-1", async ({ request }) => {
        const body = (await request.json()) as { conversation_id?: string };
        sentConversationId = body.conversation_id ?? null;
        return new HttpResponse(
          new ReadableStream({
            start(controller) {
              controller.enqueue(
                new TextEncoder().encode(sseFrame("done", makeDonePayload())),
              );
              controller.close();
            },
          }),
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        );
      }),
    );

    renderPanel(client);
    await screen.findByTestId("history-pane");
    await user.type(screen.getByTestId("chat-input-textarea"), "Ask");
    await user.keyboard("{Enter}");

    await waitFor(() => expect(sentConversationId).toBe(stored));
  });
});
