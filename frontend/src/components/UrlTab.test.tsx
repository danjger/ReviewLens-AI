/**
 * Integration smoke tests for {@link UrlTab} wired through MSW.
 *
 * Covers the task-9.1 flow at the container level: submitting URLs starts a
 * Check (and blocks a second submission while running, Requirement 1.5),
 * polling fills in verdict cards, the include defaults follow Requirement 3.8,
 * the `?check=` query string is written for persistence, and a 429 surfaces the
 * resume message (Requirement 1.6). Comprehensive cases are task 9.4.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import UrlTab from "./UrlTab";

function resetUrl() {
  window.history.pushState(null, "", "/");
}

beforeEach(resetUrl);
afterEach(resetUrl);

describe("UrlTab", () => {
  it("checks URLs, polls to a verdict, and persists ?check=", async () => {
    const user = userEvent.setup();

    server.use(
      http.post("/api/ingest/checks", () =>
        HttpResponse.json(
          {
            check_id: "chk-1",
            items: [
              { item_id: "u1", input: "https://example.com/a", normalized: "https://example.com/a", state: "pending" },
            ],
          },
          { status: 202 },
        ),
      ),
      http.get("/api/ingest/checks/chk-1", () =>
        HttpResponse.json({
          check_id: "chk-1",
          created_at: "2026-09-02T00:00:00Z",
          origin: "new",
          items: [
            {
              item_id: "u1",
              input: "https://example.com/a",
              normalized: "https://example.com/a",
              final_url: "https://example.com/a",
              state: "done",
              hops: [],
              verdict: {
                verdict: "will_work",
                reasons: ["24 reviews found and verified on this page"],
                warnings: [],
                evidence: {
                  reviews_verified: 24,
                  reviews_rejected: 0,
                  method: "selectors",
                  pagination: true,
                  reported_total: 1540,
                  blocker: null,
                  locator_confidence: "high",
                  page_title: "Acme",
                  main_status: 200,
                  samples: [{ text: "Great product.", rating: 5, date: "2026-09-02" }],
                },
              },
              existing_dataset: null,
            },
          ],
        }),
      ),
    );

    renderWithClient(<UrlTab />);

    await user.type(screen.getByTestId("url-input-textarea"), "https://example.com/a");
    await user.click(screen.getByTestId("check-button"));

    // The verdict card appears once polling resolves the item.
    const card = await screen.findByTestId("verdict-card");
    expect(within(card).getByTestId("verdict-badge")).toHaveAttribute("data-verdict", "will_work");

    // Included by default for a non-wont_work verdict (Requirement 3.8).
    await waitFor(() =>
      expect(within(card).getByTestId("include-checkbox")).toBeChecked(),
    );

    // ?check= persisted for reload (design: panel keeps check_id in the URL).
    expect(new URLSearchParams(window.location.search).get("check")).toBe("chk-1");
  });

  it("shows the 429 resume message when checks are rate-limited (Requirement 1.6)", async () => {
    const user = userEvent.setup();

    server.use(
      http.post("/api/ingest/checks", () =>
        HttpResponse.json(
          { error: { code: "RATE_LIMITED", message: "Too many requests." } },
          { status: 429, headers: { "Retry-After": "45" } },
        ),
      ),
    );

    renderWithClient(<UrlTab />);

    await user.type(screen.getByTestId("url-input-textarea"), "https://example.com/a");
    await user.click(screen.getByTestId("check-button"));

    const msg = await screen.findByTestId("rate-limit-message");
    expect(msg).toHaveTextContent("45 seconds");
  });

  it("restores results from an existing ?check= on mount", async () => {
    window.history.pushState(null, "", "/?check=chk-9");

    server.use(
      http.get("/api/ingest/checks/chk-9", () =>
        HttpResponse.json({
          check_id: "chk-9",
          created_at: "2026-09-02T00:00:00Z",
          origin: "new",
          items: [
            {
              item_id: "u1",
              input: "https://example.com/restored",
              normalized: "https://example.com/restored",
              final_url: "https://example.com/restored",
              state: "done",
              hops: [],
              verdict: {
                verdict: "limited",
                reasons: ["Only 2 reviews found"],
                warnings: ["This page is disallowed for crawlers by robots.txt"],
                evidence: {
                  reviews_verified: 2,
                  reviews_rejected: 0,
                  method: "ai_direct",
                  pagination: false,
                  reported_total: null,
                  blocker: null,
                  locator_confidence: "medium",
                  page_title: "Restored",
                  main_status: 200,
                  samples: [],
                },
              },
              existing_dataset: null,
            },
          ],
        }),
      ),
    );

    renderWithClient(<UrlTab />);

    const card = await screen.findByTestId("verdict-card");
    expect(within(card).getByTestId("verdict-badge")).toHaveAttribute("data-verdict", "limited");
    // The robots warning is shown (Requirement 3.12).
    expect(within(card).getByTestId("card-warning")).toBeInTheDocument();
  });

  it("surfaces invalid and duplicate lines from the initial POST (Requirements 1.2, 1.4)", async () => {
    const user = userEvent.setup();

    server.use(
      http.post("/api/ingest/checks", () =>
        HttpResponse.json(
          {
            check_id: "chk-mix",
            items: [
              { item_id: "u1", input: "not-a-url", normalized: null, state: "invalid", message: "This line is not a valid http or https URL." },
              { item_id: "u2", input: "https://example.com/a", normalized: "https://example.com/a", state: "duplicate_in_batch", message: "Duplicate of another URL in this submission." },
              { item_id: "u3", input: "https://example.com/a", normalized: "https://example.com/a", state: "pending" },
            ],
          },
          { status: 202 },
        ),
      ),
      // Polling resolves the one real line; the invalid/duplicate stay terminal.
      http.get("/api/ingest/checks/chk-mix", () =>
        HttpResponse.json({
          check_id: "chk-mix",
          created_at: "2026-09-02T00:00:00Z",
          origin: "new",
          items: [
            { item_id: "u1", input: "not-a-url", normalized: null, final_url: null, state: "invalid", hops: [], verdict: null, existing_dataset: null, message: "This line is not a valid http or https URL." },
            { item_id: "u2", input: "https://example.com/a", normalized: "https://example.com/a", final_url: null, state: "duplicate_in_batch", hops: [], verdict: null, existing_dataset: null, message: "Duplicate of another URL in this submission." },
            {
              item_id: "u3",
              input: "https://example.com/a",
              normalized: "https://example.com/a",
              final_url: "https://example.com/a",
              state: "done",
              hops: [],
              verdict: {
                verdict: "will_work",
                reasons: ["Reviews found"],
                warnings: [],
                evidence: {
                  reviews_verified: 7, reviews_rejected: 0, method: "selectors", pagination: false,
                  reported_total: null, blocker: null, locator_confidence: "high",
                  page_title: "A", main_status: 200, samples: [],
                },
              },
              existing_dataset: null,
            },
          ],
        }),
      ),
    );

    renderWithClient(<UrlTab />);

    await user.type(screen.getByTestId("url-input-textarea"), "not-a-url\nhttps://example.com/a\nhttps://example.com/a");
    await user.click(screen.getByTestId("check-button"));

    // The invalid and duplicate lines render their own messages, no badge.
    expect(await screen.findByTestId("card-invalid")).toHaveTextContent(/not a valid/i);
    expect(screen.getByTestId("card-duplicate")).toHaveTextContent(/duplicate/i);

    // Only the real line has a verdict badge once polling resolves it.
    await waitFor(() => expect(screen.getAllByTestId("verdict-badge")).toHaveLength(1));
  });

  it("re-queues a timed-out item via Retry and polls it to a verdict (Requirement 3.10)", async () => {
    const user = userEvent.setup();
    window.history.pushState(null, "", "/?check=chk-retry");

    let retried = false;

    server.use(
      http.post("/api/ingest/checks/chk-retry/items/u1/retry", () => {
        retried = true;
        return new HttpResponse(null, { status: 202 });
      }),
      http.get("/api/ingest/checks/chk-retry", () => {
        // Before retry: a timed-out wont_work (retryable). After retry: will_work.
        const verdict = retried
          ? {
              verdict: "will_work",
              reasons: ["Reviews found"],
              warnings: [],
              evidence: {
                reviews_verified: 9, reviews_rejected: 0, method: "selectors", pagination: true,
                reported_total: null, blocker: null, locator_confidence: "high",
                page_title: "Acme", main_status: 200, samples: [],
              },
            }
          : {
              verdict: "wont_work",
              reasons: ["Page took too long to load"],
              warnings: [],
              evidence: {
                reviews_verified: 0, reviews_rejected: 0, method: null, pagination: false,
                reported_total: null, blocker: null, locator_confidence: null,
                page_title: "", main_status: null, samples: [],
              },
            };
        return HttpResponse.json({
          check_id: "chk-retry",
          created_at: "2026-09-02T00:00:00Z",
          origin: "new",
          items: [
            {
              item_id: "u1",
              input: "https://example.com/slow",
              normalized: "https://example.com/slow",
              final_url: "https://example.com/slow",
              state: "done",
              hops: [],
              verdict,
              existing_dataset: null,
            },
          ],
        });
      }),
    );

    renderWithClient(<UrlTab />);

    // The timed-out card offers Retry.
    const retry = await screen.findByTestId("retry-link");
    await user.click(retry);

    // After the retry request + re-poll, the item flips to will_work.
    await waitFor(() =>
      expect(screen.getByTestId("verdict-badge")).toHaveAttribute("data-verdict", "will_work"),
    );
    expect(retried).toBe(true);
  });

  it("blocks a second Check submission while one is running (Requirement 1.5)", async () => {
    const user = userEvent.setup();

    server.use(
      http.post("/api/ingest/checks", () =>
        HttpResponse.json(
          {
            check_id: "chk-run",
            items: [
              { item_id: "u1", input: "https://example.com/a", normalized: "https://example.com/a", state: "pending" },
            ],
          },
          { status: 202 },
        ),
      ),
      // Item stays pending → the Check keeps "running", so the button stays disabled.
      http.get("/api/ingest/checks/chk-run", () =>
        HttpResponse.json({
          check_id: "chk-run",
          created_at: "2026-09-02T00:00:00Z",
          origin: "new",
          items: [
            { item_id: "u1", input: "https://example.com/a", normalized: "https://example.com/a", final_url: null, state: "checking", hops: [], verdict: null, existing_dataset: null },
          ],
        }),
      ),
    );

    renderWithClient(<UrlTab />);
    await user.type(screen.getByTestId("url-input-textarea"), "https://example.com/a");
    await user.click(screen.getByTestId("check-button"));

    // While the single item is still in flight, the Check button is disabled.
    await waitFor(() => expect(screen.getByTestId("check-button")).toBeDisabled());
    expect(screen.getByTestId("check-button")).toHaveTextContent("Checking…");
  });
});
