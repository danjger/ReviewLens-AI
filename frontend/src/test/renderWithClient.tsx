/**
 * renderWithClient — render a component inside a fresh TanStack Query client.
 *
 * Each call creates its own `QueryClient` with retries disabled, so tests are
 * isolated and fast (no retry back-off, no shared cache between cases).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";

export function makeTestClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

export function renderWithClient(
  ui: ReactElement,
  options?: Omit<RenderOptions, "wrapper">,
): RenderResult & { client: QueryClient } {
  const client = makeTestClient();
  const result = render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
    options,
  );
  return { ...result, client };
}
