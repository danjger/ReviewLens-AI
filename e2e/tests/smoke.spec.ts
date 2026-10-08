import { test, expect } from "@playwright/test";
import { baseUrl } from "../support/env";

/**
 * Smoke E2E: open the app without signing in.
 *
 * Validates Requirement 2.1 — the Portal does not require sign-in; every page
 * is usable without credentials. The real guarantee available today is that the
 * app shell loads directly at the base URL with no login redirect or auth gate.
 *
 * The "empty dataset library" view (data-testid="dataset-library-empty") is
 * built in the `dataset-library` spec. Until then this test asserts the app
 * shell loads unauthenticated, and performs a SOFT check for the empty-library
 * testid so it does not spuriously hard-fail against the current minimal
 * frontend. When dataset-library lands, promote that soft check to a hard
 * assertion.
 *
 * Per testing.md: use data-testid selectors; never assert on text containing
 * counts or dates.
 */
test.describe("smoke", () => {
  test("app loads without signing in", async ({ page }) => {
    const response = await page.goto("/");

    // The app must be reachable and return a successful document.
    expect(response, "no response from base URL").not.toBeNull();
    expect(response!.ok(), "base URL did not return a successful response").toBe(
      true,
    );

    // No login wall: we land on the app shell, not an auth/login redirect.
    // (Requirement 2.1 — usable without credentials.)
    expect(page.url(), "navigation was redirected to a login page").not.toMatch(
      /\/(login|signin|sign-in|auth)\b/i,
    );

    // The app shell renders. This is the stable selector available today.
    await expect(page.getByTestId("app-shell")).toBeVisible();

    // Soft check: the empty dataset library is delivered by the
    // `dataset-library` spec. Don't hard-fail the smoke test on it yet.
    const emptyLibrary = page.getByTestId("dataset-library-empty");
    if ((await emptyLibrary.count()) > 0) {
      await expect
        .soft(emptyLibrary, "empty dataset library should be visible")
        .toBeVisible();
    } else {
      test.info().annotations.push({
        type: "deferred",
        description:
          'data-testid="dataset-library-empty" is added by the dataset-library spec; ' +
          "promote this to a hard assertion once that UI exists.",
      });
    }
  });

  test("harness is configured", async () => {
    // Sanity: the base URL used by this run (helps triage env misconfig).
    expect(baseUrl, "E2E_BASE_URL / default base URL must be set").toBeTruthy();
  });
});
