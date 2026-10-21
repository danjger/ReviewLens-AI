/**
 * Property-based test for the real-time cache reducers (dataset-library task 8).
 *
 * Property 4: Live updates converge.
 *   For any sequence of `dataset.status.changed` events delivered in any order,
 *   applying them with the cache reducer SHALL produce the same row state as
 *   applying only the latest event for each dataset.
 *   Validates: Requirements 6.3, 6.4
 *
 * The reducer `applyDatasetStatusChanged` is an identity-keyed patch: it
 * overwrites the matching row's live fields with the frame's fields. The
 * always-present fields (status, last_message, data_version, active_version)
 * take the value of the LAST frame seen for that dataset; `review_count` is
 * STICKY — the reducer only overwrites it when a frame actually carries a
 * `metrics.review_count`, so it keeps the value of the last frame that supplied
 * one (design `datasetPatchFields`: the server-derived count is preserved
 * across frames that omit it). This test drives arbitrary sequences of frames
 * (over a fixed pool of dataset ids all present in the `['datasets']` cache)
 * through the REAL `applyRealtimeFrame` dispatch and asserts the full sequence
 * converges to the row an independent fold produces: latest frame per dataset
 * for the always-present fields, latest count-bearing frame for review_count.
 *
 * Uses fast-check (frontend property testing, per the design) with Vitest.
 */
import { QueryClient } from "@tanstack/react-query";
import fc from "fast-check";
import { describe, expect, it } from "vitest";

import {
  applyRealtimeFrame,
  DATASETS_QUERY_KEY,
  terminalDisplayState,
} from "./realtimeReducers";
import type { DatasetRow, DatasetStatusChangedFrame } from "./realtimeTypes";

// A small fixed pool of dataset ids. Keeping the pool fixed (and seeding every
// id into the cache up front) means every frame patches a known row rather than
// triggering the "unknown id → invalidate list" path, so the test isolates the
// convergence property of the patch itself.
const DATASET_IDS = ["ds-1", "ds-2", "ds-3"] as const;

const STATUSES = ["requested", "processing", "updated", "failed"] as const;

/** Arbitrary for a single `dataset.status.changed` frame over the id pool. */
const frameArb: fc.Arbitrary<DatasetStatusChangedFrame> = fc.record({
  type: fc.constant("dataset.status.changed" as const),
  dataset_id: fc.constantFrom(...DATASET_IDS),
  status: fc.constantFrom(...STATUSES),
  at: fc.date({ min: new Date("2020-01-01"), max: new Date("2030-01-01") }).map((d) =>
    d.toISOString(),
  ),
  data_version: fc.option(fc.integer({ min: 0, max: 20 }), { nil: null }),
  active_version: fc.option(fc.integer({ min: 1, max: 20 }), { nil: null }),
  message: fc.string({ maxLength: 24 }),
  metrics: fc.oneof(
    fc.constant<Record<string, unknown>>({}),
    fc.record({ review_count: fc.integer({ min: 0, max: 999 }) }),
  ),
});

/**
 * Independent oracle: fold a dataset's frames (in delivery order) the way the
 * reducer does. Always-present fields take the latest frame's value;
 * `review_count` is sticky — it keeps the latest value a frame actually
 * carried; `display_state` is settled from the latest frame when it is terminal
 * (issue #10), a pure function of that frame so convergence still holds.
 * Returns the seeded bare row when no frame targeted the id.
 */
function expectedRow(id: string, frames: DatasetStatusChangedFrame[]): DatasetRow {
  const mine = frames.filter((f) => f.dataset_id === id);
  if (mine.length === 0) return { id };

  const last = mine[mine.length - 1];
  const row: DatasetRow = {
    id,
    status: last.status,
    last_message: last.message,
    data_version: last.data_version,
    active_version: last.active_version,
  };
  // `display_state` is settled in place by a TERMINAL frame (issue #10) and is
  // STICKY like review_count: the reducer's patch only SETS display_state on a
  // terminal frame and never clears it, so a later non-terminal frame leaves a
  // previously-settled badge in place. The oracle therefore keeps the value of
  // the latest frame that DERIVED a terminal display_state. It is a pure
  // function of that frame, so convergence (Property 4) still holds.
  for (let i = mine.length - 1; i >= 0; i -= 1) {
    const settled = terminalDisplayState(mine[i].status, mine[i].active_version);
    if (settled !== undefined) {
      row.display_state = settled;
      break;
    }
  }
  // Latest frame that supplied a numeric review_count wins; if none did, the
  // field is never set (stays absent on the seeded row).
  for (let i = mine.length - 1; i >= 0; i -= 1) {
    if (typeof mine[i].metrics.review_count === "number") {
      row.review_count = mine[i].metrics.review_count as number;
      break;
    }
  }
  return row;
}

/** Seed every id as a bare row so each frame patches (never invalidates). */
function seed(client: QueryClient): void {
  client.setQueryData<DatasetRow[]>(
    [...DATASETS_QUERY_KEY],
    DATASET_IDS.map((id) => ({ id })),
  );
}

function applyAll(frames: DatasetStatusChangedFrame[]): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  seed(client);
  for (const frame of frames) applyRealtimeFrame(client, frame);
  return client;
}

describe("Property 4: live updates converge", () => {
  it("the full sequence converges to the latest-event-per-dataset fold", () => {
    fc.assert(
      fc.property(fc.array(frameArb, { maxLength: 40 }), (frames) => {
        // Apply every frame in the delivered order through the real dispatch.
        const full = applyAll(frames);
        const fullRows = full.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])!;
        const fullById = new Map(fullRows.map((r) => [r.id, r] as const));

        // Each row equals the independent latest-event fold. This is the
        // convergence claim: the final state is a function of the latest
        // frame(s) per dataset, not of the whole delivery order.
        for (const id of DATASET_IDS) {
          expect(fullById.get(id)).toEqual(expectedRow(id, frames));
        }
      }),
    );
  });

  it("delivery order does not matter beyond the latest frame per dataset", () => {
    // For a given set of frames, any permutation that preserves each dataset's
    // last frame (and last count-bearing frame) yields the same cache. We prove
    // the weaker, always-true form: applying frames then applying each
    // dataset's latest frame again is a no-op (the latest already won).
    fc.assert(
      fc.property(fc.array(frameArb, { maxLength: 40 }), (frames) => {
        const once = applyAll(frames);
        const onceRows = once.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])!;

        const latest = new Map<string, DatasetStatusChangedFrame>();
        for (const frame of frames) latest.set(frame.dataset_id, frame);
        const again = applyAll([...frames, ...latest.values()]);
        const againRows = again.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])!;

        // Re-delivering the latest frame changes nothing it already applied.
        // (review_count is only re-asserted when that latest frame carried it,
        // which matches the single-apply result, so the rows stay equal.)
        const byId = (rows: DatasetRow[]) =>
          new Map(rows.map((r) => [r.id, r] as const));
        const onceById = byId(onceRows);
        const againById = byId(againRows);
        for (const id of DATASET_IDS) {
          expect(againById.get(id)).toEqual(onceById.get(id));
        }
      }),
    );
  });

  it("re-applying the latest frame is idempotent", () => {
    fc.assert(
      fc.property(frameArb, fc.integer({ min: 1, max: 5 }), (frame, times) => {
        const once = applyAll([frame]);
        const many = applyAll(Array.from({ length: times }, () => frame));

        const onceRows = once.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])!;
        const manyRows = many.getQueryData<DatasetRow[]>([...DATASETS_QUERY_KEY])!;
        expect(manyRows).toEqual(onceRows);
      }),
    );
  });
});
