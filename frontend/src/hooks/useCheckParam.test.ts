/**
 * Tests for {@link useCheckParam} (dataset-ingestion task 9.4).
 *
 * The design says the panel keeps the active `check_id` in the URL
 * (`?check=…`) so reloading restores the results. This hook is the mechanism:
 * it reads the current `?check=` on mount, writes/clears it through the History
 * API without a reload, and stays in sync with back/forward (`popstate`).
 *
 * These cases drive it directly (no component), asserting both the returned
 * `checkId` and the actual `window.location.search`, so the "reload keeps
 * ?check=" behaviour is pinned down at its source.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useCheckParam } from "./useCheckParam";

function resetUrl() {
  window.history.pushState(null, "", "/");
}

beforeEach(resetUrl);
afterEach(resetUrl);

describe("useCheckParam", () => {
  it("reads an existing ?check= on mount", () => {
    window.history.pushState(null, "", "/?check=chk-42");
    const { result } = renderHook(() => useCheckParam());
    expect(result.current.checkId).toBe("chk-42");
  });

  it("returns null when no ?check= is present", () => {
    const { result } = renderHook(() => useCheckParam());
    expect(result.current.checkId).toBeNull();
  });

  it("writes ?check= into the URL without a reload", () => {
    const { result } = renderHook(() => useCheckParam());

    act(() => result.current.setCheckId("chk-7"));

    expect(result.current.checkId).toBe("chk-7");
    expect(new URLSearchParams(window.location.search).get("check")).toBe("chk-7");
  });

  it("clears ?check= when set to null", () => {
    window.history.pushState(null, "", "/?check=chk-7");
    const { result } = renderHook(() => useCheckParam());

    act(() => result.current.setCheckId(null));

    expect(result.current.checkId).toBeNull();
    expect(window.location.search).toBe("");
  });

  it("preserves other query parameters when writing ?check=", () => {
    window.history.pushState(null, "", "/?tab=url");
    const { result } = renderHook(() => useCheckParam());

    act(() => result.current.setCheckId("chk-3"));

    const params = new URLSearchParams(window.location.search);
    expect(params.get("tab")).toBe("url");
    expect(params.get("check")).toBe("chk-3");
  });

  it("syncs with back/forward navigation via popstate", () => {
    const { result } = renderHook(() => useCheckParam());

    act(() => result.current.setCheckId("chk-1"));
    expect(result.current.checkId).toBe("chk-1");

    // Simulate a back navigation that drops the param, then a popstate event.
    act(() => {
      window.history.replaceState(null, "", "/");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(result.current.checkId).toBeNull();
  });
});
