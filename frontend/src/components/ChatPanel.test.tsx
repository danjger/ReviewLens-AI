/**
 * ChatPanel component tests — the guardrailed-chat task 6.9 completeness pass.
 *
 * The 6.1–6.8 subtasks each shipped FOCUSED tests for their own component
 * (HistoryPane, RefreshMarker, ChatInput, VersionNote, StreamingExchange,
 * SuggestionChips, CitationChip, DeclineTag, StaleExchangeChip, the history
 * toolbar, the realtime reducers). This file is the composition / integration
 * pass over the whole {@link ChatPanel}, filling the gaps those focused tests
 * leave — above all the headline 6.9 requirement:
 *
 *   **a live refresh-marker change from pending → completed on a mock event.**
 *
 * That live change is driven end-to-end through the real realtime chain: the
 * {@link ChatPanel} mounts {@link useRealtime}, which opens a socket (the
 * {@link MockWebSocket} helper, testing.md). A `dataset.status.changed` frame
 * delivered over that socket flows through `parseFrame` → `applyRealtimeFrame`
 * → `applyDatasetStatusChanged`, which invalidates the dataset's chat-history
 * query; the pane refetches and the pending {@link RefreshMarker}'s
 * `data-state` flips to `completed` (Requirements 9.2, 9.7). The history
 * endpoint's response changes between the two fetches to model the backend
 * seeing the version complete.
 *
 * Other composition gaps covered here (not re-asserting the focused tests):
 *  - empty history shows the suggestions prominently; a chip click prefills the
 *    input; once there is history the suggestions collapse (1.4, design);
 *  - the VersionNote shows above the input while refreshing / after a failed
 *    refresh, and is absent when available (1.1);
 *  - a full ask → stream → merge-into-history flow through the composed panel
 *    (6.2, 6.3);
 *  - a declined history Exchange shows the decline tag, and a citation chip's
 *    popover reads the saved snippet — including on a STALE Exchange, proving
 *    the popover uses saved snippets after a refresh (2.2, 3.4, 9.4);
 *  - disabled input states for archived and no-active-version (1.2, 1.3).
 *
 * The API is mocked with MSW and the WebSocket with `src/test/ws.ts`; selectors
 * are data-testid / data-* only and nothing asserts on count/date text
 * (testing.md).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { HistoryPage } from "../api/chat";
import {
  makeChatExchange,
  makeCitationSnippet,
  makeHistoryPage,
  makeRefreshMarker,
  makeSuggestions,
} from "../test/fixtures";
import { server } from "../test/server";
import {
  installMockWebSocket,
  lastMockWebSocket,
  uninstallMockWebSocket,
} from "../test/ws";
import ChatPanel from "./ChatPanel";

const DATASET_ID = "ds-1";
const HISTORY_PATH = `/api/datasets/${DATASET_ID}/chat/history`;
const SUGGESTIONS_PATH = `/api/datasets/${DATASET_ID}/chat/suggestions`;

// ── jsdom layout stubs for @tanstack/react-virtual (as in HistoryPane.test) ──
// jsdom has no layout, so the virtualizer sees zero-size elements. Give the
// scroll element a fixed height and a writable scrollTop so a window of rows
// renders and the pane's logic (not the virtualizer's pixel math) is exercised.
let scrollTopValue = 0;

class StubResizeObserver {
  constructor(private readonly cb: ResizeObserverCallback) {}
  observe(target: Element): void {
    const rect = target.getBoundingClientRect();
    this.cb(
      [
        {
          target,
          contentRect: rect,
          borderBoxSize: [{ inlineSize: rect.width, blockSize: rect.height }],
          contentBoxSize: [{ inlineSize: rect.width, blockSize: rect.height }],
          devicePixelContentBoxSize: [{ inlineSize: rect.width, blockSize: rect.height }],
        } as unknown as ResizeObserverEntry,
      ],
      this as unknown as ResizeObserver,
    );
  }
  unobserve(): void {}
  disconnect(): void {}
}

beforeEach(() => {
  scrollTopValue = 0;
  installMockWebSocket();
  try {
    window.sessionStorage.clear();
  } catch {
    /* ignore */
  }
  vi.stubGlobal("ResizeObserver", StubResizeObserver);
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get() {
      return 300;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get() {
      return 1000;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "scrollTop", {
    configurable: true,
    get() {
      return scrollTopValue;
    },
    set(v: number) {
      scrollTopValue = v;
    },
  });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width: 400,
    height: 300,
    top: 0,
    left: 0,
    right: 400,
    bottom: 300,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
});

afterEach(() => {
  uninstallMockWebSocket();
  server.resetHandlers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  const proto = HTMLElement.prototype as unknown as Record<string, unknown>;
  for (const prop of ["clientHeight", "scrollHeight", "scrollTop"] as const) {
    delete proto[prop];
  }
});

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

/** Render the panel inside a fresh client; realtime left ON so the socket opens. */
function renderPanel(
  client: QueryClient,
  props: Partial<ComponentProps<typeof ChatPanel>> = {},
) {
  return render(
    <QueryClientProvider client={client}>
      <ChatPanel datasetId={DATASET_ID} {...props} />
    </QueryClientProvider>,
  );
}

/** Default: suggestions always resolve (so composition tests don't 404). */
function stubSuggestions(suggestions?: string[]) {
  server.use(
    http.get(SUGGESTIONS_PATH, () => HttpResponse.json(makeSuggestions(suggestions))),
  );
}

/** Install a fixed history response. */
function stubHistory(page: HistoryPage) {
  server.use(http.get(HISTORY_PATH, () => HttpResponse.json(page)));
}

// ───────────────────────────────────────────────────────────────────────────
// THE HEADLINE: a live pending → completed marker change on a mock event
// (Requirements 9.2, 9.7).
// ───────────────────────────────────────────────────────────────────────────

describe("ChatPanel — live refresh marker pending → completed (Req 9.2, 9.7)", () => {
  it("flips a pending RefreshMarker to completed on a dataset.status.changed frame", async () => {
    const client = makeClient();
    stubSuggestions();

    // The history endpoint returns a PENDING v2 marker on the first fetch; once
    // the refresh completes (modelled by the live frame triggering a refetch),
    // it returns the COMPLETED v2 marker instead — exactly what the backend
    // history API does when `dataset_versions` v2 moves to `updated`.
    let completed = false;
    server.use(
      http.get(HISTORY_PATH, () => {
        const marker = completed
          ? makeRefreshMarker({
              version: 2,
              state: "completed",
              completed_at: "2026-09-03T08:20:00Z",
              review_count: 212,
              previous_review_count: 180,
            })
          : makeRefreshMarker({
              version: 2,
              state: "pending",
              completed_at: null,
              review_count: null,
              previous_review_count: null,
            });
        return HttpResponse.json(
          makeHistoryPage([makeChatExchange({ id: "ex-1" }), marker]),
        );
      }),
    );

    renderPanel(client);

    // Initially the pending "Refreshing data…" marker shows (Req 9.2).
    await waitFor(() =>
      expect(screen.getByTestId("refresh-marker")).toHaveAttribute("data-state", "pending"),
    );
    expect(screen.getByTestId("refresh-marker-pending")).toBeInTheDocument();

    // The real-time channel is open (useRealtime mounted inside the panel).
    const socket = lastMockWebSocket();
    expect(socket).toBeDefined();
    act(() => socket!.simulateOpen());

    // The backend has now finished v2; the next history fetch returns completed.
    completed = true;

    // Deliver a `dataset.status.changed` frame moving the dataset to `updated`
    // (refresh completed). This flows through useRealtime → the reducer →
    // invalidate the chat-history query → refetch (Req 9.7).
    act(() => {
      socket!.simulateMessage({
        type: "dataset.status.changed",
        dataset_id: DATASET_ID,
        status: "updated",
        at: "2026-09-03T08:20:00Z",
        data_version: 2,
        active_version: 2,
        message: "Processed 212 reviews",
        metrics: { review_count: 212 },
      });
    });

    // After the invalidation-driven refetch, the SAME marker is now completed
    // (Req 9.2: "It SHALL become a completed marker when the refresh finishes").
    await waitFor(() =>
      expect(screen.getByTestId("refresh-marker")).toHaveAttribute("data-state", "completed"),
    );
    expect(screen.getByTestId("refresh-marker-completed")).toBeInTheDocument();
    // v2 completed; the before/after counts are exposed as data-* (not text).
    const marker = screen.getByTestId("refresh-marker");
    expect(marker).toHaveAttribute("data-version", "2");
    expect(marker).toHaveAttribute("data-review-count", "212");
    expect(marker).toHaveAttribute("data-previous-review-count", "180");
  });

  it("flips a pending marker to failed on a failed-refresh frame (Req 9.3, 9.7)", async () => {
    const client = makeClient();
    stubSuggestions();

    let failed = false;
    server.use(
      http.get(HISTORY_PATH, () => {
        const marker = failed
          ? makeRefreshMarker({ version: 2, state: "failed", completed_at: null })
          : makeRefreshMarker({ version: 2, state: "pending", completed_at: null });
        return HttpResponse.json(makeHistoryPage([marker]));
      }),
    );

    renderPanel(client);

    await waitFor(() =>
      expect(screen.getByTestId("refresh-marker")).toHaveAttribute("data-state", "pending"),
    );

    const socket = lastMockWebSocket()!;
    act(() => socket.simulateOpen());
    failed = true;
    act(() => {
      socket.simulateMessage({
        type: "dataset.status.changed",
        dataset_id: DATASET_ID,
        status: "failed",
        at: "2026-09-03T08:30:00Z",
        data_version: 1,
        active_version: 1,
        message: "Refresh failed",
        metrics: {},
      });
    });

    await waitFor(() =>
      expect(screen.getByTestId("refresh-marker")).toHaveAttribute("data-state", "failed"),
    );
    expect(screen.getByTestId("refresh-marker-failed")).toBeInTheDocument();
  });

  it("ignores a frame for a different dataset (no refetch, marker stays pending)", async () => {
    const client = makeClient();
    stubSuggestions();

    let fetches = 0;
    server.use(
      http.get(HISTORY_PATH, () => {
        fetches += 1;
        return HttpResponse.json(
          makeHistoryPage([makeRefreshMarker({ version: 2, state: "pending" })]),
        );
      }),
    );

    renderPanel(client);
    await waitFor(() =>
      expect(screen.getByTestId("refresh-marker")).toHaveAttribute("data-state", "pending"),
    );
    const fetchesAfterLoad = fetches;

    const socket = lastMockWebSocket()!;
    act(() => socket.simulateOpen());
    act(() => {
      socket.simulateMessage({
        type: "dataset.status.changed",
        dataset_id: "ds-OTHER",
        status: "updated",
        at: "2026-09-03T08:20:00Z",
        data_version: 2,
        active_version: 2,
        message: "done",
        metrics: {},
      });
    });

    // No refetch for a different dataset: the fetch count is unchanged and the
    // marker is still pending.
    await Promise.resolve();
    expect(fetches).toBe(fetchesAfterLoad);
    expect(screen.getByTestId("refresh-marker")).toHaveAttribute("data-state", "pending");
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Suggestions composition: empty prominent → prefill → collapsed with history
// (Requirement 1.4; design "Frontend (ChatPanel)").
// ───────────────────────────────────────────────────────────────────────────

describe("ChatPanel — suggestions presentation + prefill (Req 1.4)", () => {
  it("shows suggestions prominently when the history is empty", async () => {
    const client = makeClient();
    stubSuggestions();
    stubHistory(makeHistoryPage([]));

    renderPanel(client);

    const chips = await screen.findByTestId("suggestion-chips");
    // Empty history → prominent (not collapsed).
    expect(chips).toHaveAttribute("data-collapsed", "false");
    expect(screen.getAllByTestId("suggestion-chip").length).toBeGreaterThan(0);
  });

  it("prefills the input with a clicked suggestion (not auto-submit)", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    stubSuggestions(["What do reviewers say about customer support?"]);
    stubHistory(makeHistoryPage([]));

    renderPanel(client);

    const chip = await screen.findByTestId("suggestion-chip");
    const chipText = chip.textContent ?? "";
    await user.click(chip);

    // The question is lifted into the input value (prefill), ready to edit/send.
    const textarea = screen.getByTestId("chat-input-textarea") as HTMLTextAreaElement;
    await waitFor(() => expect(textarea.value).toBe(chipText));
    // No stream started: the panel is still idle (not auto-submitted).
    expect(screen.getByTestId("streaming-exchange")).toHaveAttribute("data-phase", "idle");
  });

  it("collapses the suggestions once there is history", async () => {
    const client = makeClient();
    stubSuggestions();
    stubHistory(makeHistoryPage([makeChatExchange({ id: "ex-1" })]));

    renderPanel(client);

    const chips = await screen.findByTestId("suggestion-chips");
    await waitFor(() => expect(chips).toHaveAttribute("data-collapsed", "true"));
  });
});

// ───────────────────────────────────────────────────────────────────────────
// VersionNote placement from availability (Requirement 1.1).
// ───────────────────────────────────────────────────────────────────────────

describe("ChatPanel — VersionNote from availability (Req 1.1)", () => {
  it("shows the refreshing note above the input while a refresh runs", async () => {
    const client = makeClient();
    stubSuggestions();
    stubHistory(makeHistoryPage([]));

    renderPanel(client, { availability: "refreshing", activeVersion: 2 });

    const note = await screen.findByTestId("version-note");
    expect(note).toHaveAttribute("data-state", "refreshing");
    expect(note).toHaveAttribute("data-active-version", "2");
  });

  it("shows the refresh-failed note after a failed refresh", async () => {
    const client = makeClient();
    stubSuggestions();
    stubHistory(makeHistoryPage([]));

    renderPanel(client, { availability: "refresh_failed", activeVersion: 3 });

    const note = await screen.findByTestId("version-note");
    expect(note).toHaveAttribute("data-state", "refresh_failed");
    expect(note).toHaveAttribute("data-active-version", "3");
  });

  it("shows no VersionNote when the chat is simply available", async () => {
    const client = makeClient();
    stubSuggestions();
    stubHistory(makeHistoryPage([]));

    renderPanel(client, { availability: "available", activeVersion: 3 });

    await screen.findByTestId("chat-panel");
    expect(screen.queryByTestId("version-note")).not.toBeInTheDocument();
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Input disabled states through the panel (Requirements 1.2, 1.3).
// ───────────────────────────────────────────────────────────────────────────

describe("ChatPanel — disabled availability states (Req 1.2, 1.3)", () => {
  it("disables the input and shows the archived message when archived", async () => {
    const client = makeClient();
    stubSuggestions();
    stubHistory(makeHistoryPage([makeChatExchange({ id: "ex-1" })]));

    renderPanel(client, { availability: "archived" });

    await screen.findByTestId("chat-panel");
    const textarea = screen.getByTestId("chat-input-textarea") as HTMLTextAreaElement;
    expect(textarea.disabled).toBe(true);
    const msg = screen.getByTestId("chat-input-disabled-message");
    expect(msg).toHaveAttribute("data-availability", "archived");
    // The archived history is still visible (Req 1.3).
    expect(screen.getByTestId("history-pane")).toBeInTheDocument();
  });

  it("disables the input when there is no active version", async () => {
    const client = makeClient();
    stubSuggestions();
    stubHistory(makeHistoryPage([]));

    renderPanel(client, { availability: "no_active_version" });

    await screen.findByTestId("chat-panel");
    const textarea = screen.getByTestId("chat-input-textarea") as HTMLTextAreaElement;
    expect(textarea.disabled).toBe(true);
    expect(screen.getByTestId("chat-input-disabled-message")).toHaveAttribute(
      "data-availability",
      "no_active_version",
    );
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Citation popover + decline tag + stale chip, composed in the panel's history
// (Requirements 2.2, 3.4, 9.4).
// ───────────────────────────────────────────────────────────────────────────

describe("ChatPanel — citations, decline tag, stale chip (Req 2.2, 3.4, 9.4)", () => {
  it("reveals a citation popover reading the saved snippet on a stale Exchange", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    stubSuggestions();
    // A stale (older-version) Exchange whose citation carries a saved snippet;
    // the popover must read that snippet, proving it still works after a refresh.
    stubHistory(
      makeHistoryPage([
        makeChatExchange({
          id: "ex-stale",
          data_version: 1,
          is_stale: true,
          citations: ["r_0042"],
          citation_snippets: {
            r_0042: makeCitationSnippet({ text: "Checkout was painfully slow.", rating: 2 }),
          },
        }),
      ]),
    );

    renderPanel(client);

    const row = await screen.findByTestId("history-exchange");
    // Stale styling + chip (Req 9.4).
    expect(row).toHaveAttribute("data-stale", "true");
    expect(within(row).getByTestId("stale-chip")).toHaveAttribute("data-version", "1");

    // The citation chip's popover reads the SAVED snippet text (Req 2.2).
    const chipButton = within(row).getByTestId("citation-chip-button");
    await user.hover(chipButton);
    const popover = await within(row).findByTestId("citation-popover");
    expect(within(popover).getByTestId("citation-popover-text")).toHaveTextContent(
      "Checkout was painfully slow.",
    );
  });

  it("shows the decline tag on a declined Exchange", async () => {
    const client = makeClient();
    stubSuggestions();
    stubHistory(
      makeHistoryPage([
        makeChatExchange({
          id: "ex-declined",
          scope: "declined",
          scope_category: "world_knowledge",
          citations: [],
          citation_snippets: {},
        }),
      ]),
    );

    renderPanel(client);

    const row = await screen.findByTestId("history-exchange");
    expect(row).toHaveAttribute("data-scope", "declined");
    expect(within(row).getByTestId("decline-tag")).toHaveAttribute(
      "data-scope-category",
      "world_knowledge",
    );
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Full ask → stream → merge-into-history flow through the composed panel
// (Requirements 6.2, 6.3).
// ───────────────────────────────────────────────────────────────────────────

describe("ChatPanel — ask → stream → merge into history (Req 6.2, 6.3)", () => {
  it("streams an answer then merges the Exchange into the history timeline", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    stubSuggestions();
    stubHistory(makeHistoryPage([]));

    const sseFrame = (event: string, data: unknown): string =>
      `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
    const donePayload = {
      ...(() => {
        const base = makeChatExchange({ id: "ex-new", question: "Q?", answer: "Streamed answer." });
        const { type, is_stale, ...rest } = base;
        void type;
        void is_stale;
        return rest;
      })(),
      saved: true,
      signature: "deadbeef",
    };

    server.use(
      http.post(`/api/chat/datasets/${DATASET_ID}`, () => {
        const stream = new ReadableStream({
          start(controller) {
            const enc = new TextEncoder();
            controller.enqueue(enc.encode(sseFrame("token", { text: "Streamed " })));
            controller.enqueue(enc.encode(sseFrame("token", { text: "answer." })));
            controller.enqueue(enc.encode(sseFrame("done", donePayload)));
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
    await screen.findByTestId("chat-panel");

    await user.type(screen.getByTestId("chat-input-textarea"), "What's up?");
    await user.keyboard("{Enter}");

    // The pending row clears to idle and the new Exchange lands in the history.
    await waitFor(() =>
      expect(screen.getByTestId("streaming-exchange")).toHaveAttribute("data-phase", "idle"),
    );
    await waitFor(() => {
      const rows = screen.getAllByTestId("history-exchange");
      expect(rows.some((r) => r.getAttribute("data-exchange-id") === "ex-new")).toBe(true);
    });
    // The input was cleared on completion (Req 6.3).
    const textarea = screen.getByTestId("chat-input-textarea") as HTMLTextAreaElement;
    expect(textarea.value).toBe("");
  });
});
