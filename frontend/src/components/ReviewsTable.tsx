/**
 * ReviewsTable — the paginated sample-reviews table on the detail page
 * (ingestion-summary task 5, Requirements 5.1, 5.2).
 *
 * THE Detail Page provides a paginated table of the extracted reviews with
 * text (truncated, expandable), rating, date, author, and sentiment
 * (Requirement 5.1), and supports filtering by rating and sentiment plus a text
 * search (Requirement 5.2).
 *
 * **Server-paginated and server-filtered (design "ReviewsTable":
 * "server-paginated, with filters and debounced search").** The table never
 * filters or slices in the browser. The current page and the rating / sentiment
 * / search filters are passed to {@link useReviews}, which calls
 * `GET /datasets/{id}/reviews?page&page_size&rating&sentiment&q`; the backend
 * reads the active-version `reviews/v{n}.json`, applies the filters, and returns
 * the already-paginated page as `{items, total, page}`. The pager is driven by
 * `total` and `page` from that response.
 *
 * **Debounced search (Requirement 5.2).** Typing updates a local input value
 * immediately (so the field stays responsive) but only commits to the query
 * param after a short idle delay, so a burst of keystrokes triggers one refetch
 * rather than one per character. Changing a filter or committing a search
 * resets back to page 1 so the analyst never lands on an out-of-range page.
 *
 * **Review text is source text.** The `text` shown here is copied verbatim from
 * the review object the backend returns (which the review-analysis stage read
 * from the page); it is rendered as-is and never generated. Long text is
 * truncated with a per-row expand/collapse toggle (Requirement 5.1).
 *
 * Every control and cell carries a `data-testid`; the pager position is exposed
 * via `data-*` attributes so tests assert paging/filtering behaviour without
 * depending on formatted counts or dates (testing.md).
 */
import { useEffect, useMemo, useRef, useState } from "react";

import type { ReviewItem, ReviewSentiment } from "../api/datasets";
import { useReviews } from "../hooks/useDatasets";

export interface ReviewsTableProps {
  /** The dataset id from the `/datasets/{id}` route. */
  datasetId: string;
  /** Items per page (defaults to the server's 25). */
  pageSize?: number;
  /** Debounce for the text search, in ms (overridable so tests can shorten it). */
  searchDebounceMs?: number;
}

/** Default rows per page (matches the backend `REVIEWS_DEFAULT_PAGE_SIZE`). */
const DEFAULT_PAGE_SIZE = 25;

/** Default search debounce; a burst of keystrokes commits once it goes idle. */
const DEFAULT_SEARCH_DEBOUNCE_MS = 300;

/** Characters of review text shown before the row offers "Show more". */
const TRUNCATE_AT = 220;

/** The selectable sentiment filter values (plus the "any" sentinel). */
const SENTIMENTS: ReviewSentiment[] = ["positive", "neutral", "negative"];

/** The selectable rating filter values (plus the "any" sentinel). */
const RATINGS = [5, 4, 3, 2, 1] as const;

/** Human label for each sentiment (icon-free; text so it is not colour-alone). */
const SENTIMENT_LABEL: Record<ReviewSentiment, string> = {
  positive: "Positive",
  neutral: "Neutral",
  negative: "Negative",
};

/** One review row: text (truncated + expandable), rating, date, author, sentiment. */
function ReviewRow({ review }: { review: ReviewItem }) {
  const [expanded, setExpanded] = useState(false);
  const isLong = review.text.length > TRUNCATE_AT;
  const shown = !isLong || expanded ? review.text : `${review.text.slice(0, TRUNCATE_AT)}…`;

  return (
    <tr className="reviews-table__row" data-testid="review-row" data-review-id={review.id}>
      <td className="reviews-table__cell reviews-table__cell--text">
        {review.title != null && review.title.length > 0 && (
          <p className="reviews-table__title" data-testid="review-title">
            {review.title}
          </p>
        )}
        <p className="reviews-table__text" data-testid="review-text" data-expanded={expanded}>
          {shown}
        </p>
        {isLong && (
          <button
            type="button"
            className="reviews-table__expand"
            data-testid="review-expand"
            aria-expanded={expanded}
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? "Show less" : "Show more"}
          </button>
        )}
      </td>
      <td className="reviews-table__cell reviews-table__cell--rating" data-testid="review-rating">
        {review.rating != null ? (
          <span aria-label={`${review.rating} out of 5 stars`}>{review.rating}★</span>
        ) : (
          <span className="reviews-table__empty">—</span>
        )}
      </td>
      <td className="reviews-table__cell reviews-table__cell--date" data-testid="review-date">
        {review.date != null && review.date.length > 0 ? (
          <time dateTime={review.date}>{review.date}</time>
        ) : (
          <span className="reviews-table__empty">—</span>
        )}
      </td>
      <td className="reviews-table__cell reviews-table__cell--author" data-testid="review-author">
        {review.author != null && review.author.length > 0 ? (
          review.author
        ) : (
          <span className="reviews-table__empty">—</span>
        )}
      </td>
      <td className="reviews-table__cell reviews-table__cell--sentiment">
        <span
          className="reviews-table__sentiment"
          data-testid="review-sentiment"
          data-sentiment={review.sentiment}
        >
          {SENTIMENT_LABEL[review.sentiment] ?? review.sentiment}
        </span>
      </td>
    </tr>
  );
}

export default function ReviewsTable({
  datasetId,
  pageSize = DEFAULT_PAGE_SIZE,
  searchDebounceMs = DEFAULT_SEARCH_DEBOUNCE_MS,
}: ReviewsTableProps) {
  // Committed filter/page state → drives the server query.
  const [page, setPage] = useState(1);
  const [rating, setRating] = useState<number | null>(null);
  const [sentiment, setSentiment] = useState<ReviewSentiment | null>(null);
  // The committed (debounced) search term that is actually sent to the server.
  const [searchTerm, setSearchTerm] = useState("");
  // The live text-input value (updates on every keystroke; debounced into
  // `searchTerm`). Kept separate so the field stays responsive.
  const [searchInput, setSearchInput] = useState("");

  // Debounce the search input into the committed `searchTerm`: a burst of
  // keystrokes commits once, after `searchDebounceMs` of idle. Committing a new
  // term also resets to page 1 (below) so the analyst doesn't land out of range.
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (debounceRef.current != null) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setSearchTerm((prev) => {
        if (prev === searchInput) return prev;
        setPage(1);
        return searchInput;
      });
    }, searchDebounceMs);
    return () => {
      if (debounceRef.current != null) clearTimeout(debounceRef.current);
    };
  }, [searchInput, searchDebounceMs]);

  const params = useMemo(
    () => ({
      page,
      page_size: pageSize,
      rating,
      sentiment,
      q: searchTerm.trim().length > 0 ? searchTerm.trim() : undefined,
    }),
    [page, pageSize, rating, sentiment, searchTerm],
  );

  const query = useReviews(datasetId, params);
  const data = query.data;
  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  // The server echoes the page it served; fall back to our requested page.
  const currentPage = data?.page ?? page;
  const hasPrev = currentPage > 1;
  const hasNext = currentPage < totalPages;

  // Changing a filter resets to page 1 (the new filter likely changes the
  // match count, so staying on page N could land out of range).
  const onRatingChange = (value: string): void => {
    setRating(value === "" ? null : Number(value));
    setPage(1);
  };
  const onSentimentChange = (value: string): void => {
    setSentiment(value === "" ? null : (value as ReviewSentiment));
    setPage(1);
  };

  return (
    <section className="reviews-table" data-testid="reviews-table" aria-label="Sample reviews">
      <h2 className="reviews-table__heading">Sample reviews</h2>

      <div className="reviews-table__filters" data-testid="reviews-filters">
        <label className="reviews-table__filter">
          <span className="reviews-table__filter-label">Rating</span>
          <select
            data-testid="reviews-filter-rating"
            value={rating == null ? "" : String(rating)}
            onChange={(event) => onRatingChange(event.target.value)}
          >
            <option value="">All ratings</option>
            {RATINGS.map((value) => (
              <option key={value} value={String(value)}>
                {value}★
              </option>
            ))}
          </select>
        </label>

        <label className="reviews-table__filter">
          <span className="reviews-table__filter-label">Sentiment</span>
          <select
            data-testid="reviews-filter-sentiment"
            value={sentiment ?? ""}
            onChange={(event) => onSentimentChange(event.target.value)}
          >
            <option value="">All sentiment</option>
            {SENTIMENTS.map((value) => (
              <option key={value} value={value}>
                {SENTIMENT_LABEL[value]}
              </option>
            ))}
          </select>
        </label>

        <label className="reviews-table__filter reviews-table__filter--search">
          <span className="reviews-table__filter-label">Search</span>
          <input
            type="search"
            data-testid="reviews-filter-search"
            placeholder="Search review text"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
          />
        </label>
      </div>

      <table className="reviews-table__table">
        <thead>
          <tr>
            <th scope="col">Review</th>
            <th scope="col">Rating</th>
            <th scope="col">Date</th>
            <th scope="col">Author</th>
            <th scope="col">Sentiment</th>
          </tr>
        </thead>
        <tbody data-testid="reviews-body">
          {items.map((review) => (
            <ReviewRow key={review.id} review={review} />
          ))}
        </tbody>
      </table>

      {query.isError && (
        <p className="reviews-table__error" data-testid="reviews-error" role="alert">
          {query.error.message}
        </p>
      )}

      {!query.isError && items.length === 0 && (
        <p className="reviews-table__empty-state" data-testid="reviews-empty" role="status">
          No reviews match these filters.
        </p>
      )}

      <nav
        className="reviews-table__pager"
        data-testid="reviews-pager"
        data-page={currentPage}
        data-total-pages={totalPages}
        data-total={total}
        aria-label="Reviews pages"
      >
        <button
          type="button"
          data-testid="reviews-prev"
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          disabled={!hasPrev}
        >
          Previous
        </button>
        <span className="reviews-table__pager-status" data-testid="reviews-page-indicator">
          Page {currentPage} of {totalPages}
        </span>
        <button
          type="button"
          data-testid="reviews-next"
          onClick={() => setPage((p) => (hasNext ? p + 1 : p))}
          disabled={!hasNext}
        >
          Next
        </button>
      </nav>
    </section>
  );
}
