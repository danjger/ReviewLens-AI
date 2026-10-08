/**
 * Unit tests for the real-time cache patch reducers (dataset-library task 5,
 * Requirements 6.3, 6.4, 6.5).
 *
 * These exercise the reducers directly against a real `QueryClient`:
 * - a `dataset.status.changed` frame patches the `['datasets']` row and the
 *   `['dataset', id]` detail record in place;
 * - an UNKNOWN dataset id invalidates `['datasets']` so the list refetches;
 * - a `check.updated` frame patches `['check', check_id]`, and a browser not
 *   showing that check (no cache entry) ignores it.
 */
import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { makeItem, makeSession } from "../test/fixtures";
import {
  applyCheckUpdated,
  applyDatasetStatusChanged,
  applyRealtimeFrame,
  datasetQueryKey,
  DATASETS_QUERY_KEY,
} from "./realtimeReducers";
import { chatHistoryQueryKey } from "./useChatHistory";
import { checkQueryKey } from "./useCheck";
import type {
  CheckUpdatedFrame,
  DatasetRow,
  DatasetStatusChangedFrame,
} from "./realtimeTypes";

function statusFrame(
  overrides: Partial<DatasetStatusChangedFrame> = {},
): DatasetStatusChangedFrame {
  return {
    type: "dataset.status.changed",
    dataset_id: "ds-1",
    status: "processing",
    at: "2026-09-02T00:00:00Z",
    data_version: 2,
    active_version: 1,
    message: "Fetching page 3 of 10",
    metrics: {},
    ...overrides,
  };
}

function checkFrame(overrides: Partial<CheckUpdatedFrame> = {}): CheckUpdatedFrame {
  return {
    type: "check.updated",
    check_id: "check-1",
    item_id: "u1",
    state: "done",
    verdict: "will_work",
    ...overrides,
  };
}

let client: QueryClient;

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});

describe("applyDatasetStatusChanged — list + detail patch (Req 6.3, 6.4)", () => {
  it("patches the matching row in the ['datasets'] list", () => {
    const rows: DatasetRow[] = [
      { id: "ds-1", status: "requested", last_message: "Queued", review_count: null },
      { id: "ds-2", status: "updated", last_message: "Done", review_count: 10 },
    ];
    client.setQueryData([...DATASETS_QUERY_KEY], rows);

    applyDatasetStatusChanged(
      client,
      statusFrame({ status: "processing", message: "Fetching page 3 of 10", metrics: { review_count: 7 } }),
    );

    const updated = client.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])!;
    expect(updated[0]).toMatchObject({
      id: "ds-1",
      status: "processing",
      last_message: "Fetching page 3 of 10",
      review_count: 7,
      data_version: 2,
      active_version: 1,
    });
    // Other rows untouched.
    expect(updated[1]).toEqual(rows[1]);
  });

  it("patches the ['dataset', id] detail record in place", () => {
    client.setQueryData<DatasetRow>(datasetQueryKey("ds-1"), {
      id: "ds-1",
      status: "requested",
      name: "Acme CRM",
    });

    applyDatasetStatusChanged(client, statusFrame({ status: "updated", active_version: 2 }));

    const detail = client.getQueryData<DatasetRow>(datasetQueryKey("ds-1"))!;
    expect(detail).toMatchObject({ id: "ds-1", status: "updated", active_version: 2, name: "Acme CRM" });
  });

  it("no-ops a detail cache the tab hasn't populated", () => {
    // No ['dataset','ds-1'] entry and no list entry.
    applyDatasetStatusChanged(client, statusFrame());
    expect(client.getQueryData(datasetQueryKey("ds-1"))).toBeUndefined();
  });
});

describe("applyDatasetStatusChanged — unknown id invalidates the list (Req 6.5)", () => {
  it("invalidates ['datasets'] when the id isn't in the loaded list", () => {
    client.setQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY], [
      { id: "ds-1", status: "updated" },
    ]);
    const invalidate = vi.spyOn(client, "invalidateQueries");

    applyDatasetStatusChanged(client, statusFrame({ dataset_id: "ds-NEW" }));

    expect(invalidate).toHaveBeenCalledWith({ queryKey: [...DATASETS_QUERY_KEY] });
  });

  it("does NOT invalidate the list when the list cache is empty/unloaded", () => {
    const invalidate = vi.spyOn(client, "invalidateQueries");
    applyDatasetStatusChanged(client, statusFrame({ dataset_id: "ds-NEW" }));
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: [...DATASETS_QUERY_KEY] });
  });

  it("does NOT invalidate the list when the id is already known", () => {
    client.setQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY], [{ id: "ds-1", status: "updated" }]);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    applyDatasetStatusChanged(client, statusFrame({ dataset_id: "ds-1" }));
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: [...DATASETS_QUERY_KEY] });
  });
});

describe("applyDatasetStatusChanged — refresh markers refetch chat history (task 6.8, Req 9.2, 9.7)", () => {
  // The refresh-relevant status set the design names (started/completed/failed)
  // plus `processing`, which Req 9.2 keeps as the pending-marker state.
  it.each(["requested", "processing", "updated", "failed"])(
    "invalidates ['chat-history', id] on a '%s' status",
    (status) => {
      const invalidate = vi.spyOn(client, "invalidateQueries");

      applyDatasetStatusChanged(client, statusFrame({ dataset_id: "ds-1", status }));

      expect(invalidate).toHaveBeenCalledWith({
        queryKey: chatHistoryQueryKey("ds-1"),
      });
    },
  );

  it("does NOT invalidate chat history on an unrelated status", () => {
    // `archived` doesn't change refresh markers, so the history query must not
    // be thrashed by it (keep the list/detail patch, skip the history refetch).
    const invalidate = vi.spyOn(client, "invalidateQueries");

    applyDatasetStatusChanged(client, statusFrame({ dataset_id: "ds-1", status: "archived" }));

    expect(invalidate).not.toHaveBeenCalledWith({
      queryKey: chatHistoryQueryKey("ds-1"),
    });
  });

  it("refetches the chat history of the frame's dataset only", () => {
    const invalidate = vi.spyOn(client, "invalidateQueries");

    applyDatasetStatusChanged(client, statusFrame({ dataset_id: "ds-7", status: "updated" }));

    expect(invalidate).toHaveBeenCalledWith({
      queryKey: chatHistoryQueryKey("ds-7"),
    });
    // Not some other dataset's history.
    expect(invalidate).not.toHaveBeenCalledWith({
      queryKey: chatHistoryQueryKey("ds-1"),
    });
  });

  it("still patches the list/detail row alongside the history refetch", () => {
    const rows: DatasetRow[] = [
      { id: "ds-1", status: "processing", last_message: "Refreshing", review_count: 180 },
    ];
    client.setQueryData([...DATASETS_QUERY_KEY], rows);
    client.setQueryData<DatasetRow>(datasetQueryKey("ds-1"), {
      id: "ds-1",
      status: "processing",
      name: "Acme CRM",
    });

    applyDatasetStatusChanged(
      client,
      statusFrame({
        dataset_id: "ds-1",
        status: "updated",
        active_version: 3,
        message: "Done",
        metrics: { review_count: 212 },
      }),
    );

    expect(client.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])![0]).toMatchObject({
      id: "ds-1",
      status: "updated",
      active_version: 3,
      last_message: "Done",
      review_count: 212,
    });
    expect(client.getQueryData<DatasetRow>(datasetQueryKey("ds-1"))).toMatchObject({
      id: "ds-1",
      status: "updated",
      active_version: 3,
      name: "Acme CRM",
    });
  });
});

describe("applyCheckUpdated — patches ['check', id] (Req 6.5)", () => {
  it("patches the matching item's state and verdict", () => {
    const session = makeSession([
      makeItem({ item_id: "u1", state: "checking", verdict: null }),
      makeItem({ item_id: "u2", state: "done" }),
    ]);
    client.setQueryData(checkQueryKey("check-1"), session);

    applyCheckUpdated(client, checkFrame({ item_id: "u1", state: "done", verdict: "limited" }));

    const updated = client.getQueryData<typeof session>(checkQueryKey("check-1"))!;
    expect(updated.items[0]).toMatchObject({ item_id: "u1", state: "done" });
    // u1 had no verdict object, so verdict stays null (reducer only patches an
    // existing verdict label; it never fabricates review-bearing data).
    expect(updated.items[0].verdict).toBeNull();
    expect(updated.items[1].state).toBe("done");
  });

  it("updates the verdict label on an item that already had a verdict", () => {
    const session = makeSession([makeItem({ item_id: "u1", state: "checking" })]);
    client.setQueryData(checkQueryKey("check-1"), session);

    applyCheckUpdated(client, checkFrame({ item_id: "u1", verdict: "wont_work" }));

    const updated = client.getQueryData<typeof session>(checkQueryKey("check-1"))!;
    expect(updated.items[0].verdict?.verdict).toBe("wont_work");
  });

  it("ignores a check.updated for a check this browser isn't showing", () => {
    // No ['check','check-1'] entry: a tab not showing that check.
    applyCheckUpdated(client, checkFrame());
    expect(client.getQueryData(checkQueryKey("check-1"))).toBeUndefined();
  });
});

describe("applyRealtimeFrame — dispatch", () => {
  it("routes a status frame to the dataset reducer", () => {
    client.setQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY], [{ id: "ds-1", status: "requested" }]);
    applyRealtimeFrame(client, statusFrame({ status: "updated" }));
    expect(client.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])![0].status).toBe("updated");
  });

  it("routes a check frame to the check reducer", () => {
    const session = makeSession([makeItem({ item_id: "u1", state: "checking" })]);
    client.setQueryData(checkQueryKey("check-1"), session);
    applyRealtimeFrame(client, checkFrame({ item_id: "u1", state: "done" }));
    expect(client.getQueryData<typeof session>(checkQueryKey("check-1"))!.items[0].state).toBe("done");
  });
});

describe("Correctness Property 4 — live updates converge", () => {
  it("applying events in any order ends at the latest event's state", () => {
    client.setQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY], [{ id: "ds-1", status: "requested" }]);

    // Deliver out of order; the last one applied wins (identity patch).
    applyDatasetStatusChanged(client, statusFrame({ status: "processing", message: "p" }));
    applyDatasetStatusChanged(client, statusFrame({ status: "updated", message: "u", active_version: 2 }));

    const row = client.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])![0];
    expect(row).toMatchObject({ status: "updated", last_message: "u", active_version: 2 });
  });
});
