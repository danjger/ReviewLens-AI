/**
 * Unit tests for the streaming chat client (guardrailed-chat task 6.4):
 * the SSE buffer parser, the `done → ChatExchange` adapter, and the
 * `streamChat` fetch-streaming reader. Component-level behaviour is covered in
 * `StreamingExchange.test.tsx`.
 */
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "../test/server";
import {
  doneToExchange,
  parseSseBuffer,
  streamChat,
  type SavedExchange,
  type ChatStreamEvent,
} from "./chat";

function donePayload(): SavedExchange {
  return {
    id: "ex-1",
    dataset_id: "ds-1",
    data_version: 2,
    conversation_id: "conv-1",
    asked_at: "2026-09-02T12:00:00Z",
    answered_at: "2026-09-02T12:00:03Z",
    question: "Q?",
    answer: "A [r_0001].",
    citations: ["r_0001"],
    dropped_citations: 0,
    citation_snippets: { r_0001: { text: "t", rating: 5, date: "2026-08-14" } },
    scope: "in_scope",
    scope_category: null,
    saved: true,
    signature: "deadbeef",
  };
}

function sse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

describe("parseSseBuffer", () => {
  it("parses whole token/done frames and returns no remainder", () => {
    const buffer = sse("token", { text: "Hi" }) + sse("done", donePayload());
    const { events, rest } = parseSseBuffer(buffer);
    expect(rest).toBe("");
    expect(events.map((e) => e.type)).toEqual(["token", "done"]);
    expect((events[0] as Extract<ChatStreamEvent, { type: "token" }>).text).toBe("Hi");
  });

  it("leaves an unterminated trailing frame as the remainder", () => {
    const buffer = sse("token", { text: "one" }) + "event: token\ndata: {\"text\":\"par";
    const { events, rest } = parseSseBuffer(buffer);
    expect(events).toHaveLength(1);
    expect(rest).toContain("par");
  });

  it("parses an error frame into {code,message}", () => {
    const { events } = parseSseBuffer(sse("error", { code: "X", message: "boom" }));
    expect(events[0]).toEqual({ type: "error", code: "X", message: "boom" });
  });

  it("skips unknown event names and malformed JSON", () => {
    const buffer = sse("comment", { x: 1 }) + "event: token\ndata: {bad json\n\n";
    const { events } = parseSseBuffer(buffer);
    expect(events).toHaveLength(0);
  });
});

describe("doneToExchange", () => {
  it("adapts a done payload into a non-stale timeline Exchange", () => {
    const ex = doneToExchange(donePayload());
    expect(ex.type).toBe("exchange");
    expect(ex.is_stale).toBe(false);
    expect(ex.id).toBe("ex-1");
    // Streaming-only fields are not part of a timeline row.
    expect("saved" in ex).toBe(false);
    expect("signature" in ex).toBe(false);
  });
});

describe("streamChat", () => {
  it("dispatches token events then done from a streamed body", async () => {
    const chunks = [sse("token", { text: "a" }), sse("token", { text: "b" }), sse("done", donePayload())];
    server.use(
      http.post("/api/chat/datasets/ds-1", () => {
        const stream = new ReadableStream({
          start(controller) {
            const enc = new TextEncoder();
            for (const c of chunks) controller.enqueue(enc.encode(c));
            controller.close();
          },
        });
        return new HttpResponse(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }),
    );

    const tokens: string[] = [];
    let done: SavedExchange | null = null;
    await streamChat(
      "ds-1",
      { question: "Q?", conversation_id: "conv-1" },
      {
        onToken: (t) => tokens.push(t),
        onDone: (e) => {
          done = e;
        },
        onError: () => {
          throw new Error("unexpected error event");
        },
      },
    );

    expect(tokens.join("")).toBe("ab");
    expect(done).not.toBeNull();
    expect((done as unknown as SavedExchange).id).toBe("ex-1");
  });

  it("throws an ApiError on a 429 before streaming", async () => {
    server.use(
      http.post("/api/chat/datasets/ds-1", () =>
        HttpResponse.json(
          { error: { code: "RATE_LIMITED", message: "slow down" } },
          { status: 429, headers: { "Retry-After": "12" } },
        ),
      ),
    );

    await expect(
      streamChat(
        "ds-1",
        { question: "Q?", conversation_id: "c" },
        { onToken: () => {}, onDone: () => {}, onError: () => {} },
      ),
    ).rejects.toMatchObject({ status: 429, retryAfterSeconds: 12 });
  });
});
