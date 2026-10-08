/**
 * useCheckParam — keep the active `check_id` in the URL's `?check=` query string
 * so reloading the page restores the Check results (dataset-ingestion design:
 * "The panel keeps the current `check_id` in the URL").
 *
 * This hook reads and writes `window.location`'s query string through the
 * History API directly rather than through a router, so the URL tab can be
 * mounted standalone (and in tests) without a `<Router>`. It stays in sync with
 * back/forward navigation by listening for `popstate`.
 */
import { useCallback, useEffect, useState } from "react";

/** The query-string key that carries the active check id. */
export const CHECK_PARAM = "check";

/** Read the current `?check=` value from `window.location`. */
function readCheckParam(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get(CHECK_PARAM);
}

export interface UseCheckParamResult {
  /** The current `check_id` from the URL, or null when none is set. */
  checkId: string | null;
  /** Write (or clear, with `null`) the `?check=` value without a reload. */
  setCheckId: (checkId: string | null) => void;
}

export function useCheckParam(): UseCheckParamResult {
  const [checkId, setCheckIdState] = useState<string | null>(() => readCheckParam());

  // Keep in sync with browser back/forward.
  useEffect(() => {
    function onPopState() {
      setCheckIdState(readCheckParam());
    }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const setCheckId = useCallback((next: string | null) => {
    const params = new URLSearchParams(window.location.search);
    if (next == null) {
      params.delete(CHECK_PARAM);
    } else {
      params.set(CHECK_PARAM, next);
    }
    const query = params.toString();
    const url = `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`;
    window.history.pushState(null, "", url);
    setCheckIdState(next);
  }, []);

  return { checkId, setCheckId };
}
