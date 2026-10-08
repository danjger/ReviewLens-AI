/**
 * Focused tests for the history toolbar wired into {@link HistoryPane}
 * (guardrailed-chat task 6.7, Requirement 9.5).
 *
 * Proves the pane + toolbar + collapse compose correctly:
 *   - collapsing folds earlier-version Exchanges into one summary row per
 *     version (markers and current-version Exchanges stay), and expanding a
 *     version restores its Exchanges;
 *   - the jump buttons drive the scroll container (scroll callback invoked).
 *
 * The API is mocked with MSW; selectors are data-testid / data-* only and
 * counts are read from `data-count`, never the formatted copy (testing.md).
 * The comprehensive ChatPanel coverage is task 6.9.
 *
 * jsdom has no layout, so we stub the scroll element's dimensions and a
 * ResizeObserver (as HistoryPane.test.tsx does) so `@tanstack/react-virtual`
 * renders a window of rows.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { HistoryPage, TimelineItem } from "../api/chat";
import { makeChatExchange, makeHistoryPage, makeRefreshMarker } from "../test/fixtures";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import HistoryPane from "./HistoryPane";

const DATASET_ID = "ds-1";
const HISTORY_PATH = `/api/datasets/${DATASET_ID}/chat/history`;

/** Install a one-page history handler returning the given timeline. */
function stubHistory(page: HistoryPage): void {
  server.use(http.get(HISTORY_PATH, () => HttpResponse.json(page)));
}

let scrollTopValue = 0;
const scrollToSpy = vi.fn();

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
  scrollToSpy.mockClear();
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
  // react-virtual scrolls by calling the scroll element's scrollTo; spy on it
  // so a jump can be observed under jsdom (which does not implement it).
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {
    configurable: true,
    writable: true,
    value: scrollToSpy,
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
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  const proto = HTMLElement.prototype as unknown as Record<string, unknown>;
  for (const prop of ["clientHeight", "scrollHeight", "scrollTop", "scrollTo"] as const) {
    delete proto[prop];
  }
});

/** A timeline with two earlier versions, markers, and a current version. */
function multiVersionTimeline(): TimelineItem[] {
  return [
    makeChatExchange({ id: "v1-a", data_version: 1, is_stale: true }),
    makeChatExchange({ id: "v1-b", data_version: 1, is_stale: true }),
    makeRefreshMarker({ version: 2, state: "completed" }),
    makeChatExchange({ id: "v2-a", data_version: 2, is_stale: true }),
    makeRefreshMarker({ version: 3, state: "completed" }),
    makeChatExchange({ id: "cur", data_version: 3, is_stale: false }),
  ];
}

describe("HistoryPane toolbar — collapse earlier versions (Req 9.5)", () => {
  it("folds earlier versions into summary rows and keeps markers + current", async () => {
    stubHistory(makeHistoryPage(multiVersionTimeline()));
    const user = userEvent.setup();
    renderWithClient(<HistoryPane datasetId={DATASET_ID} />);

    await waitFor(() =>
      expect(screen.getAllByTestId("history-exchange").length).toBeGreaterThan(0),
    );

    // Before collapsing: all four Exchanges are present.
    expect(screen.getAllByTestId("history-exchange")).toHaveLength(4);
    expect(screen.queryAllByTestId("history-collapsed-version")).toHaveLength(0);

    await user.click(screen.getByTestId("history-collapse-toggle"));

    // After collapsing: one summary row per earlier version (v1 count 2, v2
    // count 1), both markers still shown, only the current Exchange expanded.
    await waitFor(() =>
      expect(screen.getAllByTestId("history-collapsed-version")).toHaveLength(2),
    );
    const summaries = screen.getAllByTestId("history-collapsed-version");
    const byVersion = new Map(
      summaries.map((el) => [el.getAttribute("data-version"), el.getAttribute("data-count")]),
    );
    expect(byVersion.get("1")).toBe("2");
    expect(byVersion.get("2")).toBe("1");

    // Markers survive; only the current-version Exchange remains expanded.
    expect(screen.getAllByTestId("refresh-marker")).toHaveLength(2);
    const remaining = screen.getAllByTestId("history-exchange");
    expect(remaining).toHaveLength(1);
    expect(remaining[0]).toHaveAttribute("data-version", "3");
  });

  it("expanding a collapsed version restores its Exchanges", async () => {
    stubHistory(makeHistoryPage(multiVersionTimeline()));
    const user = userEvent.setup();
    renderWithClient(<HistoryPane datasetId={DATASET_ID} />);

    await waitFor(() =>
      expect(screen.getAllByTestId("history-exchange").length).toBeGreaterThan(0),
    );
    await user.click(screen.getByTestId("history-collapse-toggle"));
    await waitFor(() =>
      expect(screen.getAllByTestId("history-collapsed-version")).toHaveLength(2),
    );

    // Expand v1 via its per-version "show" toggle.
    const v1Summary = screen
      .getAllByTestId("history-collapsed-version")
      .find((el) => el.getAttribute("data-version") === "1")!;
    await user.click(within(v1Summary).getByTestId("history-collapsed-expand"));

    // v1's two Exchanges are back; v2 stays folded.
    await waitFor(() =>
      expect(screen.getAllByTestId("history-collapsed-version")).toHaveLength(1),
    );
    const v1Exchanges = screen
      .getAllByTestId("history-exchange")
      .filter((el) => el.getAttribute("data-version") === "1");
    expect(v1Exchanges).toHaveLength(2);
    expect(screen.getByTestId("history-collapsed-version")).toHaveAttribute(
      "data-version",
      "2",
    );
  });

  it("disables the collapse toggle when there are no earlier versions", async () => {
    stubHistory(
      makeHistoryPage([makeChatExchange({ id: "cur", data_version: 1, is_stale: false })]),
    );
    renderWithClient(<HistoryPane datasetId={DATASET_ID} />);

    await screen.findByTestId("history-exchange");
    expect(screen.getByTestId("history-collapse-toggle")).toBeDisabled();
  });
});

describe("HistoryPane toolbar — jump to refresh (Req 9.5)", () => {
  it("scrolls the pane when a jump button is pressed", async () => {
    stubHistory(makeHistoryPage(multiVersionTimeline()));
    const user = userEvent.setup();
    renderWithClient(<HistoryPane datasetId={DATASET_ID} />);

    await waitFor(() =>
      expect(screen.getAllByTestId("history-exchange").length).toBeGreaterThan(0),
    );

    // Markers exist, so the jump buttons are enabled.
    const next = screen.getByTestId("history-jump-next");
    const prev = screen.getByTestId("history-jump-prev");
    expect(next).toBeEnabled();
    expect(prev).toBeEnabled();

    scrollToSpy.mockClear();
    await act(async () => {
      await user.click(next);
    });

    // Jumping to a marker drives the virtualizer, which scrolls the container.
    expect(scrollToSpy).toHaveBeenCalled();
  });

  it("disables jump buttons when the timeline has no markers", async () => {
    stubHistory(
      makeHistoryPage([
        makeChatExchange({ id: "a", data_version: 1, is_stale: false }),
        makeChatExchange({ id: "b", data_version: 1, is_stale: false }),
      ]),
    );
    renderWithClient(<HistoryPane datasetId={DATASET_ID} />);

    await waitFor(() =>
      expect(screen.getAllByTestId("history-exchange").length).toBeGreaterThan(0),
    );
    expect(screen.getByTestId("history-jump-next")).toBeDisabled();
    expect(screen.getByTestId("history-jump-prev")).toBeDisabled();
  });
});
