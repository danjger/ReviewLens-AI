/**
 * Unit tests for the polling-fallback helpers (dataset-library task 5,
 * design "Error Handling": list every 10 s while a row is in flight, check
 * every 2 s while running).
 */
import { describe, expect, it } from "vitest";

import { makeItem, makeSession } from "../test/fixtures";
import {
  CHECK_POLL_INTERVAL_MS,
  LIST_POLL_INTERVAL_MS,
  checkRefetchInterval,
  listRefetchInterval,
} from "./realtimePolling";
import type { DatasetRow } from "./realtimeTypes";

describe("listRefetchInterval", () => {
  it("polls at 10 s while any row is requested or processing", () => {
    const rows: DatasetRow[] = [
      { id: "a", status: "updated" },
      { id: "b", status: "processing" },
    ];
    expect(listRefetchInterval(rows)).toBe(LIST_POLL_INTERVAL_MS);
    expect(LIST_POLL_INTERVAL_MS).toBe(10_000);
  });

  it("stops once every row has settled", () => {
    const rows: DatasetRow[] = [
      { id: "a", status: "updated" },
      { id: "b", status: "failed" },
    ];
    expect(listRefetchInterval(rows)).toBe(false);
  });

  it("stops for an empty or undefined list", () => {
    expect(listRefetchInterval([])).toBe(false);
    expect(listRefetchInterval(undefined)).toBe(false);
  });
});

describe("checkRefetchInterval", () => {
  it("polls at 2 s before the first load", () => {
    expect(checkRefetchInterval(undefined)).toBe(CHECK_POLL_INTERVAL_MS);
    expect(CHECK_POLL_INTERVAL_MS).toBe(2_000);
  });

  it("polls at 2 s while any item is non-terminal", () => {
    const session = makeSession([
      makeItem({ item_id: "u1", state: "checking" }),
      makeItem({ item_id: "u2", state: "done" }),
    ]);
    expect(checkRefetchInterval(session)).toBe(CHECK_POLL_INTERVAL_MS);
  });

  it("stops once every item is terminal", () => {
    const session = makeSession([
      makeItem({ item_id: "u1", state: "done" }),
      makeItem({ item_id: "u2", state: "invalid" }),
    ]);
    expect(checkRefetchInterval(session)).toBe(false);
  });
});
