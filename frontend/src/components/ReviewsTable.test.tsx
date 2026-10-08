/**
 * Component tests for {@link ReviewsTable} (ingestion-summary task 5,
 * Requirements 5.1, 5.2).
 *
 * Covers: a page of reviews renders with text, rating, date, author, and
 * sentiment (Req 5.1); long text truncates with a working expand/collapse
 * toggle (Req 5.1); the rating and sentiment filters send the chosen value to
 * the server and refetch (Req 5.2); a debounced text search commits once and
 * refetches with `q` (Req 5.2); and the pager's Next/Previous move the `page`
 * param, driven by `total` and `page` from the response.
 *
 * Server-paginated: the table never filters/slices in the browser, so every
 * assertion drives the backend via MSW handlers that read the query string and
 * return the matching `{items, total, page}` — the test owns the "server" and
 * proves the component sent the right params. The API is mocked with MSW and
 * selectors use data-testid (testing.md). A short `searchDebounceMs` keeps the
 * debounce test fast on real timers.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { ReviewItem } from "../api/datasets";
import { makeReviewItem, makeReviewsPage } from "../test/fixtures";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import ReviewsTable from "./ReviewsTable";

const DATASET_ID = "ds-1";
const REVIEWS_PATH = `/api/datasets/${DATASET_ID}/reviews`;

/** The query params the component sent on the most recent reviews request. */
interface SeenParams {
  page: string | null;
  rating: string | null;
  sentiment: string | null;
  q: string | null;
}

/**
 * Install a reviews handler whose response is computed from the query params by
 * `respond`, and return a `seen()` accessor over the params of the last call.
 * This lets each test prove the component sent the right page/filter/search
 * params to the server (the table filters server-side, never in the browser).
 */
function stubReviews(
  respond: (params: SeenParams) => ReturnType<typeof makeReviewsPage>,
): () => SeenParams | null {
  let last: SeenParams | null = null;
  server.use(
    http.get(REVIEWS_PATH, ({ request }) => {
      const url = new URL(request.url);
      last = {
        page: url.searchParams.get("page"),
        rating: url.searchParams.get("rating"),
        sentiment: url.searchParams.get("sentiment"),
        q: url.searchParams.get("q"),
      };
      return HttpResponse.json(respond(last));
    }),
  );
  return () => last;
}

/** A simple page of three distinct reviews. */
function threeReviews(): ReviewItem[] {
  return [
    makeReviewItem({ id: "r-1", text: "First review", sentiment: "positive", rating: 5 }),
    makeReviewItem({ id: "r-2", text: "Second review", sentiment: "neutral", rating: 3 }),
    makeReviewItem({ id: "r-3", text: "Third review", sentiment: "negative", rating: 1 }),
  ];
}

describe("ReviewsTable — renders a page (Req 5.1)", () => {
  it("shows each review's text, rating, date, author, and sentiment", async () => {
    stubReviews(() => makeReviewsPage(threeReviews()));

    renderWithClient(<ReviewsTable datasetId={DATASET_ID} />);

    await waitFor(() =>
      expect(screen.getAllByTestId("review-row")).toHaveLength(3),
    );

    const first = screen.getAllByTestId("review-row")[0];
    expect(within(first).getByTestId("review-text")).toHaveTextContent("First review");
    expect(within(first).getByTestId("review-rating")).toHaveTextContent("5");
    expect(within(first).getByTestId("review-date")).toHaveTextContent("2026-09-02");
    expect(within(first).getByTestId("review-author")).toHaveTextContent("Alex");
    expect(within(first).getByTestId("review-sentiment")).toHaveAttribute(
      "data-sentiment",
      "positive",
    );
  });

  it("renders an empty-state message when no reviews match", async () => {
    stubReviews(() => makeReviewsPage([]));

    renderWithClient(<ReviewsTable datasetId={DATASET_ID} />);

    expect(await screen.findByTestId("reviews-empty")).toBeInTheDocument();
    expect(screen.queryAllByTestId("review-row")).toHaveLength(0);
  });
});

describe("ReviewsTable — expandable text (Req 5.1)", () => {
  it("truncates long text and expands/collapses on the toggle", async () => {
    const user = userEvent.setup();
    const longText = "x".repeat(400);
    stubReviews(() =>
      makeReviewsPage([makeReviewItem({ id: "r-long", text: longText })]),
    );

    renderWithClient(<ReviewsTable datasetId={DATASET_ID} />);

    const text = await screen.findByTestId("review-text");
    // Truncated: shorter than the full string and marked collapsed.
    expect(text).toHaveAttribute("data-expanded", "false");
    expect(text.textContent?.length ?? 0).toBeLessThan(longText.length);

    await user.click(screen.getByTestId("review-expand"));
    expect(screen.getByTestId("review-text")).toHaveAttribute("data-expanded", "true");
    expect(screen.getByTestId("review-text")).toHaveTextContent(longText);

    await user.click(screen.getByTestId("review-expand"));
    expect(screen.getByTestId("review-text")).toHaveAttribute("data-expanded", "false");
  });
});

describe("ReviewsTable — rating filter (Req 5.2)", () => {
  it("sends the chosen rating to the server and resets to page 1", async () => {
    const user = userEvent.setup();
    // Server honours the rating filter: only 5★ when rating=5 is sent.
    const seen = stubReviews((params) => {
      if (params.rating === "5") {
        return makeReviewsPage([makeReviewItem({ id: "r-5", rating: 5 })]);
      }
      return makeReviewsPage(threeReviews());
    });

    renderWithClient(<ReviewsTable datasetId={DATASET_ID} />);
    await waitFor(() => expect(screen.getAllByTestId("review-row")).toHaveLength(3));

    await user.selectOptions(screen.getByTestId("reviews-filter-rating"), "5");

    await waitFor(() => expect(seen()?.rating).toBe("5"));
    await waitFor(() => expect(screen.getAllByTestId("review-row")).toHaveLength(1));
    expect(seen()?.page).toBe("1");
  });
});

describe("ReviewsTable — sentiment filter (Req 5.2)", () => {
  it("sends the chosen sentiment to the server", async () => {
    const user = userEvent.setup();
    const seen = stubReviews((params) => {
      if (params.sentiment === "negative") {
        return makeReviewsPage([
          makeReviewItem({ id: "r-neg", sentiment: "negative", rating: 1 }),
        ]);
      }
      return makeReviewsPage(threeReviews());
    });

    renderWithClient(<ReviewsTable datasetId={DATASET_ID} />);
    await waitFor(() => expect(screen.getAllByTestId("review-row")).toHaveLength(3));

    await user.selectOptions(screen.getByTestId("reviews-filter-sentiment"), "negative");

    await waitFor(() => expect(seen()?.sentiment).toBe("negative"));
    await waitFor(() => expect(screen.getAllByTestId("review-row")).toHaveLength(1));
    expect(screen.getByTestId("review-sentiment")).toHaveAttribute(
      "data-sentiment",
      "negative",
    );
  });
});

describe("ReviewsTable — debounced search (Req 5.2)", () => {
  it("commits the search once after typing and refetches with q", async () => {
    const user = userEvent.setup();
    let calls = 0;
    const seen = stubReviews((params) => {
      calls += 1;
      if (params.q != null && params.q.length > 0) {
        return makeReviewsPage([makeReviewItem({ id: "r-q", text: "needle in a haystack" })]);
      }
      return makeReviewsPage(threeReviews());
    });

    // Short debounce so the test stays fast on real timers.
    renderWithClient(<ReviewsTable datasetId={DATASET_ID} searchDebounceMs={50} />);
    await waitFor(() => expect(screen.getAllByTestId("review-row")).toHaveLength(3));
    const callsAfterInitial = calls;

    await user.type(screen.getByTestId("reviews-filter-search"), "needle");

    // The committed q arrives once the input goes idle (debounced).
    await waitFor(() => expect(seen()?.q).toBe("needle"));
    await waitFor(() => expect(screen.getAllByTestId("review-row")).toHaveLength(1));

    // Debounced: typing six characters did not fire six searches with q set.
    // Allow the initial load plus the single committed-search refetch.
    expect(calls).toBeLessThanOrEqual(callsAfterInitial + 2);
    expect(seen()?.page).toBe("1");
  });
});

describe("ReviewsTable — pagination (Req 5.1)", () => {
  it("moves between pages with Next/Previous driven by total and page", async () => {
    const user = userEvent.setup();
    // 30 total across two pages of the default 25; the server echoes `page`
    // and returns rows for the requested page.
    const seen = stubReviews((params) => {
      const page = Number(params.page ?? "1");
      const count = page === 1 ? 25 : 5;
      const items = Array.from({ length: count }, (_, i) =>
        makeReviewItem({ id: `p${page}-r${i}`, text: `page ${page} review ${i}` }),
      );
      return makeReviewsPage(items, { total: 30, page });
    });

    renderWithClient(<ReviewsTable datasetId={DATASET_ID} />);

    const pager = await screen.findByTestId("reviews-pager");
    await waitFor(() => expect(pager).toHaveAttribute("data-page", "1"));
    expect(pager).toHaveAttribute("data-total-pages", "2");
    // On page 1: Previous disabled, Next enabled.
    expect(screen.getByTestId("reviews-prev")).toBeDisabled();
    expect(screen.getByTestId("reviews-next")).toBeEnabled();

    await user.click(screen.getByTestId("reviews-next"));
    await waitFor(() => expect(seen()?.page).toBe("2"));
    await waitFor(() =>
      expect(screen.getByTestId("reviews-pager")).toHaveAttribute("data-page", "2"),
    );
    // On the last page: Next disabled, Previous enabled.
    expect(screen.getByTestId("reviews-next")).toBeDisabled();
    expect(screen.getByTestId("reviews-prev")).toBeEnabled();

    await user.click(screen.getByTestId("reviews-prev"));
    await waitFor(() => expect(seen()?.page).toBe("1"));
    await waitFor(() =>
      expect(screen.getByTestId("reviews-pager")).toHaveAttribute("data-page", "1"),
    );
  });
});
