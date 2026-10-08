import "@testing-library/jest-dom";

import { toHaveNoViolations } from "jest-axe";
import { afterAll, afterEach, beforeAll, expect } from "vitest";

import { server } from "../src/test/server";

// Register jest-axe's `toHaveNoViolations` matcher on Vitest's `expect`, so the
// component tests can run axe accessibility checks (design "Testing Strategy /
// Accessibility": "axe checks run in the component tests"). jest-axe's matcher
// is framework-agnostic (plain `expect.extend` shape), so it attaches to
// Vitest's expect unchanged. Note: axe's colour-contrast rule is disabled under
// jsdom, so these checks catch structural/ARIA issues, not contrast.
expect.extend(toHaveNoViolations);

// Wire the shared MSW server lifecycle (testing.md: "mock the API with MSW").
// `onUnhandledRequest: "error"` makes a test fail loudly if it hits an endpoint
// it did not stub, so stubs stay honest.
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
