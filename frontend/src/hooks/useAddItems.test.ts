/**
 * Unit tests for the Add navigation decision (dataset-ingestion task 9.2,
 * Requirement 5.4).
 *
 * `navigationTarget` encodes the rule: navigate to a dataset's detail page only
 * when exactly one item resulted in a navigable dataset (created or a refresh
 * with a dataset_id); several results (or none) keep the analyst on the Library
 * with the summary.
 */
import { describe, expect, it } from "vitest";

import { makeAddResult } from "../test/fixtures";
import { navigationTarget } from "./useAddItems";

describe("navigationTarget (Requirement 5.4)", () => {
  it("returns the dataset id for a single created result", () => {
    const results = [makeAddResult("created", { item_id: "u1", dataset_id: "ds-9" })];
    expect(navigationTarget(results)).toBe("ds-9");
  });

  it("returns the dataset id for a single refresh result", () => {
    const results = [makeAddResult("refreshed", { item_id: "u1", dataset_id: "ds-7" })];
    expect(navigationTarget(results)).toBe("ds-7");
  });

  it("stays (null) when several navigable results come back", () => {
    const results = [
      makeAddResult("created", { item_id: "u1", dataset_id: "ds-1" }),
      makeAddResult("refreshed", { item_id: "u2", dataset_id: "ds-2" }),
    ];
    expect(navigationTarget(results)).toBeNull();
  });

  it("stays (null) when the only result is not navigable", () => {
    const results = [makeAddResult("refused_wont_work", { item_id: "u1" })];
    expect(navigationTarget(results)).toBeNull();
  });

  it("stays (null) for a single already_refreshing result (shown in summary)", () => {
    const results = [
      makeAddResult("already_refreshing", { item_id: "u1", dataset_id: "ds-3" }),
    ];
    expect(navigationTarget(results)).toBeNull();
  });

  it("navigates on the one navigable result among non-navigable ones", () => {
    const results = [
      makeAddResult("refused_wont_work", { item_id: "u1" }),
      makeAddResult("created", { item_id: "u2", dataset_id: "ds-5" }),
    ];
    expect(navigationTarget(results)).toBe("ds-5");
  });
});
