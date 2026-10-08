/**
 * Unit tests for the Dataset Library list client (dataset-library task 6).
 *
 * Pins the `GET /datasets` contract: the backend wraps the rows in a
 * `{"datasets": [...]}` envelope (see `backend/app/datasets/api.py`), so
 * {@link listDatasets} must unwrap that envelope and hand callers a bare
 * `Dataset[]`. A regression that returned the envelope object instead crashed
 * the Library (`rows.some is not a function`) in the browser.
 */
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { makeDataset } from "../test/fixtures";
import { server } from "../test/server";
import { listDatasets } from "./datasets";

describe("listDatasets envelope unwrapping", () => {
  it("returns the array out of a { datasets: [...] } body", async () => {
    const rows = [makeDataset({ id: "a" }), makeDataset({ id: "b" })];
    server.use(
      http.get("/api/datasets", () =>
        HttpResponse.json({ datasets: rows }, { status: 200 }),
      ),
    );

    const result = await listDatasets();

    expect(Array.isArray(result)).toBe(true);
    expect(result.map((d) => d.id)).toEqual(["a", "b"]);
  });

  it("defaults to an empty array when the envelope omits datasets", async () => {
    server.use(
      http.get("/api/datasets", () => HttpResponse.json({}, { status: 200 })),
    );

    const result = await listDatasets();

    expect(result).toEqual([]);
  });
});
