import { useEffect, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import DatasetDetailPage from "./routes/DatasetDetailPage";
import LibraryPage from "./routes/LibraryPage";

const queryClient = new QueryClient();

/** Pull a dataset id out of a `/datasets/{id}` path, else null. */
function datasetIdFromPath(path: string): string | null {
  const match = /^\/datasets\/([^/?#]+)/.exec(path);
  return match ? decodeURIComponent(match[1]) : null;
}

/**
 * Minimal path-based router.
 *
 * The app uses the History API for in-app navigation (LibraryPage /
 * DatasetRow push `/datasets/{id}` via `window.history.pushState`, and the
 * detail page pushes `/` to go back). pushState alone doesn't fire a navigation
 * event, so we expose a `navigate` helper that pushes and then re-derives the
 * route, and we also listen for `popstate` (browser back/forward and full
 * `page.goto`-style loads land here on mount). This keeps the single `/` and
 * `/datasets/{id}` routes reachable without pulling in a router dependency,
 * matching the History-API convention the pages already follow.
 */
function useRoutePath(): [string, (path: string) => void] {
  const [path, setPath] = useState<string>(() =>
    typeof window === "undefined" ? "/" : window.location.pathname,
  );

  useEffect(() => {
    const onPopState = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const navigate = (next: string) => {
    window.history.pushState(null, "", next);
    setPath(window.location.pathname);
  };

  return [path, navigate];
}

export default function App() {
  const [path, navigate] = useRoutePath();
  const datasetId = datasetIdFromPath(path);

  return (
    <QueryClientProvider client={queryClient}>
      <div data-testid="app-shell">
        <h1>ReviewLens AI</h1>
        {datasetId != null ? (
          <DatasetDetailPage datasetId={datasetId} onNavigate={navigate} />
        ) : (
          <LibraryPage onNavigate={navigate} />
        )}
      </div>
    </QueryClientProvider>
  );
}
