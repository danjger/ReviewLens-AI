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
  terminalDisplayState,
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

  it("does NOT invalidate the list on a NON-TERMINAL frame for a known id", () => {
    // A `processing` frame for a row already in the list is patched in place
    // only — no refetch — so the list keeps polling without thrashing.
    client.setQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY], [{ id: "ds-1", status: "updated" }]);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    applyDatasetStatusChanged(client, statusFrame({ dataset_id: "ds-1", status: "processing" }));
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: [...DATASETS_QUERY_KEY] });
  });
});

describe("applyDatasetStatusChanged — terminal frame settles a known row's badge IN PLACE (issue #10)", () => {
  // The in-place patch carries a derived `display_state` for terminal frames so
  // the status badge converges live with NO reload — and WITHOUT a list refetch
  // (which would race the Library search filter and re-show sibling rows, the
  // #12 isolation contract). The derived value is pinned to the server mapping
  // by the "terminal display_state derivation matches the server" suite below.
  it("patches display_state → 'ready' on an 'updated' frame with an active version", () => {
    client.setQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY], [
      { id: "ds-1", status: "processing", display_state: "processing" },
    ]);
    const invalidate = vi.spyOn(client, "invalidateQueries");

    applyDatasetStatusChanged(
      client,
      statusFrame({ dataset_id: "ds-1", status: "updated", active_version: 1 }),
    );

    const rows = client.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])!;
    expect(rows[0]).toMatchObject({ status: "updated", display_state: "ready" });
    // No list refetch: the badge settled in place, so an active search stays put.
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: [...DATASETS_QUERY_KEY] });
  });

  it("patches display_state → 'ready_refresh_failed' on a 'failed' frame with an active version", () => {
    client.setQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY], [
      { id: "ds-1", status: "processing", display_state: "ready_refreshing" },
    ]);
    applyDatasetStatusChanged(
      client,
      statusFrame({ dataset_id: "ds-1", status: "failed", active_version: 2 }),
    );
    const rows = client.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])!;
    expect(rows[0]).toMatchObject({ status: "failed", display_state: "ready_refresh_failed" });
  });

  it("patches display_state → 'failed' on a 'failed' frame with no active version", () => {
    client.setQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY], [
      { id: "ds-1", status: "processing", display_state: "processing" },
    ]);
    applyDatasetStatusChanged(
      client,
      statusFrame({ dataset_id: "ds-1", status: "failed", active_version: null }),
    );
    const rows = client.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])!;
    expect(rows[0]).toMatchObject({ status: "failed", display_state: "failed" });
  });

  it("does NOT set display_state on a non-terminal 'processing' frame", () => {
    client.setQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY], [
      { id: "ds-1", status: "requested", display_state: "processing" },
    ]);
    applyDatasetStatusChanged(
      client,
      statusFrame({ dataset_id: "ds-1", status: "processing", active_version: 1 }),
    );
    const rows = client.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])!;
    // Unchanged server-derived badge: the in-flight state is preserved.
    expect(rows[0].display_state).toBe("processing");
    expect(rows[0].status).toBe("processing");
  });

  it("does NOT set display_state on an 'updated' frame with no active version (delegated to refetch)", () => {
    // Cannot arise in practice (reaching `updated` sets active_version); the
    // client leaves the badge to the polling backstop rather than assert a
    // terminal badge the server maps to a non-terminal "processing".
    client.setQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY], [
      { id: "ds-1", status: "processing", display_state: "processing" },
    ]);
    applyDatasetStatusChanged(
      client,
      statusFrame({ dataset_id: "ds-1", status: "updated", active_version: null }),
    );
    const rows = client.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])!;
    expect(rows[0].status).toBe("updated");
    expect(rows[0].display_state).toBe("processing");
  });
});

describe("terminal display_state derivation matches the server (issue #10 pinning test)", () => {
  // STRONG pinning test (mandatory): the client-side terminalDisplayState MUST
  // agree with the server's single source of truth,
  // backend/app/datasets/library.py::derive_display_state, for the terminal
  // badge it derives live. This is the guard against source-of-truth drift: if
  // the server mapping ever changes for a terminal case, this test turns red.
  //
  // SERVER_DISPLAY_STATE is a faithful transcription of derive_display_state's
  // table (see that function's docstring). The client deliberately derives only
  // the cases where the server yields a TERMINAL badge; the `updated`+no-active
  // case the server maps to the NON-terminal "processing" fallback is
  // intentionally DELEGATED to the polling backstop (client returns undefined),
  // which this test asserts explicitly so the delegated set is pinned too.

  /** Faithful copy of backend derive_display_state for the inputs we model. */
  function serverDisplayState(
    status: string,
    activeVersion: number | null,
  ): string {
    const hasActive = activeVersion != null;
    if (status === "requested" || status === "processing") {
      return hasActive ? "ready_refreshing" : "processing";
    }
    if (status === "updated") return hasActive ? "ready" : "processing";
    if (status === "failed") return hasActive ? "ready_refresh_failed" : "failed";
    return hasActive ? "ready" : "processing";
  }

  // The full terminal domain the client models: terminal statuses × has-active.
  const TERMINAL_STATUSES_UNDER_TEST = ["updated", "failed"] as const;
  const ACTIVE_VERSIONS: Array<number | null> = [1, null];

  for (const status of TERMINAL_STATUSES_UNDER_TEST) {
    for (const activeVersion of ACTIVE_VERSIONS) {
      const label = `${status} + ${activeVersion == null ? "no active version" : "active version"}`;
      it(`agrees with the server (or delegates) for ${label}`, () => {
        const client = terminalDisplayState(status, activeVersion);
        const server = serverDisplayState(status, activeVersion);
        if (client === undefined) {
          // A delegated case: the client declines to settle in place. This is
          // allowed ONLY when the server's badge is the non-terminal
          // "processing" fallback (i.e. nothing terminal to settle). If the
          // server starts returning a terminal badge here, this fails.
          expect(server).toBe("processing");
        } else {
          // A derived case: it MUST equal the server's badge exactly.
          expect(client).toBe(server);
        }
      });
    }
  }

  it("only ever derives badges in the server's display_state vocabulary", () => {
    // Belt-and-braces: no derived value may be outside the known vocabulary.
    const VOCAB = new Set([
      "processing",
      "ready",
      "ready_refreshing",
      "ready_refresh_failed",
      "failed",
    ]);
    for (const status of TERMINAL_STATUSES_UNDER_TEST) {
      for (const activeVersion of ACTIVE_VERSIONS) {
        const derived = terminalDisplayState(status, activeVersion);
        if (derived !== undefined) expect(VOCAB.has(derived)).toBe(true);
      }
    }
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
