/**
 * Shared MSW server for component tests (testing.md: "mock the API with MSW").
 *
 * Tests import {@link server} to override handlers per-case with
 * `server.use(...)`. The lifecycle (listen / resetHandlers / close) is wired in
 * `tests/setup.ts` so every test file gets a clean set of handlers.
 *
 * The default handlers here are deliberately empty: each test declares exactly
 * the Check responses it needs, so an unhandled request fails loudly rather
 * than silently returning a stub.
 */
import { setupServer } from "msw/node";

export const server = setupServer();
