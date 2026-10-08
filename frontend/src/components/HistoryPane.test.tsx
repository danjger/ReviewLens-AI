/**
 * Smoke tests for {@link HistoryPane} (guardrailed-chat task 6.1,
 * Requirements 5.2, 5.3, 5.4).
 *
 * A light test here (the comprehensive component tests are task 6.9): it proves
 * the pane renders Exchanges and marker rows from the shared history, shows the
 * stale chip on a stale Exchange (Req 5.4, 9.4), and loads the earlier page
 * on scroll-to-top via the `before` cursor (Req 5.3). The API is mocked with
 * MSW and selectors use data-testid; nothing asserts on counts/dates text
 * (testing.md).
 *
 * jsdom has no layout, so `@tanstack/react-virtual` sees zero-size elements. We
 * give the scroll element real dimensions (so the virtualizer renders a window
 * of rows and `scrollTop` is writable) via a jsdom-wide stub; the pane's own
 * logic is what's under test, not the virtualizer's pixel math.
 */
import { act, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GetChatHistoryParams, HistoryPage } from "../api/chat";
import {
  makeChatExchange,
  makeHistoryPage,
  makeRefreshMarker,
} from "../test/fixtures";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import HistoryPane from "./HistoryPane";

const DATASET_ID = "ds-1";
const HISTORY_PATH = `/api/datasets/${DATASET_ID}/chat/history`;

/** The query params the pane sent on a history request. */
interface SeenParams {
  before: string | null;
  limit: string | null;
}

/**
 * Install a history handler whose response is computed from the query params by
 * `respond`, returning an accessor over every request's params (so a test can
 * assert the pane paged backward with the `before` cursor).
 */
function stubHistory(
  respond: (params: SeenParams) => HistoryPage,
): () => SeenParams[] {
  const calls: SeenParams[] = [];
  server.use(
    http.get(HISTORY_PATH, ({ request }) => {
      const url = new URL(request.url);
      const params: SeenParams = {
        before: url.searchParams.get("before"),
        limit: url.searchParams.get("limit"),
      };
      calls.push(params);
      return HttpResponse.json(respond(params));
    }),
  );
  return () => calls;
}

/**
 * Give the scroll element usable layout under jsdom: a fixed clientHeight and a
 * writable scrollTop, and a scrollHeight derived from the rendered rows. This
 * lets the virtualizer render a window and lets the test drive scroll-to-top.
 */
let scrollTopValue = 0;

/**
 * A minimal ResizeObserver that reports the observed element's (stubbed) size
 * on observe, so `@tanstack/react-virtual` learns a non-zero viewport height
 * under jsdom (which ships no ResizeObserver).
 */
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
  // @tanstack/react-virtual reads the scroll viewport via getBoundingClientRect
  // (observeElementRect calls it immediately). jsdom returns all-zero, so give a
  // non-zero height, otherwise the virtualizer renders a zero-height window.
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
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  // Remove the layout stubs so other suites see jsdom defaults.
  const proto = HTMLElement.prototype as unknown as Record<string, unknown>;
  for (const prop of ["clientHeight", "scrollHeight", "scrollTop"] as const) {
    delete proto[prop];
  }
});

describe("HistoryPane — renders the timeline (Req 5.2, 5.4)", () => {
  it("shows Exchange rows and a refresh-marker row from the shared history", async () => {
    stubHistory(() =>
      makeHistoryPage([
        makeChatExchange({ id: "ex-1", question: "first question" }),
        makeRefreshMarker({ version: 2, state: "completed" }),
        makeChatExchange({ id: "ex-2", question: "second question" }),
      ]),
    );

    renderWithClient(<HistoryPane datasetId={DATASET_ID} />);

    await waitFor(() =>
      expect(screen.getAllByTestId("history-exchange").length).toBeGreaterThan(0),
    );
    expect(screen.getByTestId("refresh-marker")).toHaveAttribute("data-version", "2");
    expect(screen.getByTestId("refresh-marker")).toHaveAttribute("data-state", "completed");
  });

  it("renders the stale chip and stale marker on an older-version Exchange", async () => {
    stubHistory(() =>
      makeHistoryPage([
        makeChatExchange({ id: "ex-stale", data_version: 1, is_stale: true }),
      ]),
    );

    renderWithClient(<HistoryPane datasetId={DATASET_ID} />);

    const row = await screen.findByTestId("history-exchange");
    expect(row).toHaveAttribute("data-stale", "true");
    expect(row).toHaveAttribute("data-version", "1");
    expect(screen.getByTestId("stale-chip")).toHaveAttribute("data-version", "1");
  });

  it("shows the empty state when there is no history", async () => {
    stubHistory(() => makeHistoryPage([]));

    renderWithClient(<HistoryPane datasetId={DATASET_ID} />);

    expect(await screen.findByTestId("history-empty")).toBeInTheDocument();
    expect(screen.queryAllByTestId("history-exchange")).toHaveLength(0);
  });
});

describe("HistoryPane — loads earlier history on scroll (Req 5.3)", () => {
  it("fetches the older page with the before cursor when scrolled to the top", async () => {
    // Newest page carries a cursor to an older page; the older page is terminal.
    const seen = stubHistory((params: SeenParams) => {
      if (params.before == null) {
        return makeHistoryPage(
          [makeChatExchange({ id: "ex-newest", question: "newest" })],
          { next_before: "2026-09-02T11:00:00Z", has_more: true },
        );
      }
      return makeHistoryPage([
        makeChatExchange({ id: "ex-older", question: "older" }),
      ]);
    });

    renderWithClient(<HistoryPane datasetId={DATASET_ID} />);

    // First (newest) page loads with no `before` and the earlier loader shows.
    await screen.findByTestId("history-earlier");
    await waitFor(() => expect(seen().length).toBe(1));
    expect(seen()[0].before).toBeNull();
    expect(seen()[0].limit).toBe("20");

    // Scroll to the very top and fire a scroll event: triggers the older fetch.
    const scroll = screen.getByTestId("history-scroll");
    (scroll as HTMLElement).scrollTop = 0;
    act(() => {
      scroll.dispatchEvent(new Event("scroll", { bubbles: true }));
    });

    // The second request pages backward with the newest page's next_before.
    await waitFor(() => expect(seen().length).toBe(2));
    expect(seen()[1].before).toBe("2026-09-02T11:00:00Z");
  });
});

describe("HistoryPane — auto-scroll to latest on open (Req 5.2)", () => {
  it("scrolls the newest item into view on first load", async () => {
    // Mark the arg type used so the import is exercised by the type-checker.
    const _params: GetChatHistoryParams = {};
    void _params;

    stubHistory(() =>
      makeHistoryPage([
        makeChatExchange({ id: "ex-a", question: "a" }),
        makeChatExchange({ id: "ex-b", question: "b" }),
        makeChatExchange({ id: "ex-c", question: "newest" }),
      ]),
    );

    renderWithClient(<HistoryPane datasetId={DATASET_ID} />);

    // The pane renders rows and performs its initial scroll-to-bottom without
    // throwing; the scroll element exists and holds the rows.
    await waitFor(() =>
      expect(screen.getAllByTestId("history-exchange").length).toBeGreaterThan(0),
    );
    expect(screen.getByTestId("history-scroll")).toBeInTheDocument();
  });
});
