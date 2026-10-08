/**
 * Typed client for the Dataset Library endpoints (dataset-library task 6).
 *
 * These mirror the design's "API endpoints" table and the backend
 * `datasets_router` built in tasks 1–3:
 *
 *   GET  /datasets?archived=&sort=&q=        list the tracked datasets
 *   GET  /datasets/{id}                      one dataset's full record
 *   PATCH /datasets/{id}                     rename
 *   POST /datasets/{id}/archive | /restore   archive / restore
 *   POST /datasets/{id}/refresh              start a refresh Check (202 {check_id})
 *   POST /datasets/{id}/refresh/confirm      confirm a `limited` refresh verdict
 *
 * Error handling reuses {@link ApiError} from `./ingest`, so a non-2xx response
 * carries the backend `{ error: { code, message } }` envelope (and `429`
 * `Retry-After`) consistently with the Check/Upload clients.
 */

import type { Hop, Verdict } from "./ingest";
import type { DatasetRow } from "../hooks/realtimeTypes";
import { ApiError } from "./ingest";

/** The server-derived badge state (design: `display_state` table). */
export type DisplayState =
  | "processing"
  | "ready"
  | "ready_refreshing"
  | "ready_refresh_failed"
  | "failed";

/** How the list is ordered (design: `sort=activity|name|status|refreshed`). */
export type DatasetSort = "activity" | "name" | "status" | "refreshed";

/**
 * One row of `GET /datasets` (design "API endpoints"). This is the shape held
 * in the `['datasets']` list cache, so it extends the push-patchable
 * {@link DatasetRow} contract from task 5 (same `id` + live fields).
 */
export interface Dataset extends DatasetRow {
  id: string;
  name: string;
  main_url: string;
  platform: string | null;
  status: string;
  display_state: DisplayState;
  last_message: string | null;
  review_count: number | null;
  data_version: number | null;
  active_version: number | null;
  requested_at: string | null;
  last_refreshed_at: string | null;
  archived_at: string | null;
  refresh_check_id: string | null;
}

/** One version row inside the detail record (design: `dataset_versions`). */
export interface DatasetVersion {
  version: number;
  status: string;
  review_count: number | null;
  completed_at: string | null;
}

/** How a dataset's reviews were obtained (backend `SourceType`). */
export type SourceType = "url" | "upload";

/**
 * One entry in the append-only `status_detail.events` log (backend
 * `app/db/status.py::_build_event`). The {@link ProcessingTimeline}
 * (ingestion-summary task 6.1, Requirement 6.1) renders these.
 *
 * Mirrors the producer exactly: `status` is the lifecycle status this event
 * recorded (`null` for a progress-only `log_event`), `at` is the ISO-8601 UTC
 * instant, `message` is the human-readable detail, and `data` is the optional
 * structured payload. `message` and `at` are always written; `status` and
 * `data` may be `null`/absent, so consumers tolerate missing keys.
 */
export interface StatusEvent {
  /** The lifecycle status this event recorded, or null for a progress event. */
  status?: string | null;
  /** The ISO-8601 UTC instant the event was appended. */
  at?: string | null;
  /** The human-readable progress/status message. */
  message?: string | null;
  /** Optional structured data stored alongside the event. */
  data?: Record<string, unknown>;
}

/**
 * The append-only event log + redirect/viability detail (backend
 * `datasets.status_detail`). The detail page reads `events` (ProcessingTimeline,
 * ingestion-summary task 6.1), `viability` (PredictionPanel, task 4.4), and the
 * `redirects` hops surfaced by the {@link DatasetHeader} (Requirement 1.2).
 *
 * Every field is optional: a freshly-`requested` dataset has only `events`, an
 * upload has no `redirects`, and the detail endpoint may omit the block
 * entirely. Consumers must tolerate missing keys.
 */
export interface StatusDetail {
  /**
   * The append-only status/progress event log (backend
   * `status_detail.events`), oldest first as written. Each {@link StatusEvent}
   * carries a `status`, `at`, `message`, and optional `data`.
   */
  events?: StatusEvent[];
  /** The redirect hops recorded during the probe/capture (Requirement 1.2). */
  redirects?: Hop[];
  /**
   * The Viability Check prediction for the active version plus the actual
   * processing result (ingestion-summary Requirement 7). Written by Add /
   * refresh as the {@link Verdict} shape under `status_detail.viability`
   * (`app/ingestion/service.py`), with the `actual` block appended on every
   * completion by the review-analysis completion stage
   * (`app/handlers/completion.py::ActualResult`). Absent for uploads (no URL
   * check) and before the first version completes.
   */
  viability?: Viability;
  [key: string]: unknown;
}

/**
 * The actual processing result recorded next to the viability prediction
 * (backend `completion.ActualResult.as_dict`, Requirement 6.4). Mirrors that
 * producer exactly: reviews extracted, pages captured, the extraction method
 * used, and how many pages fell back to the Review Locator.
 */
export interface ViabilityActual {
  /** Number of reviews extracted and stored. */
  reviews: number;
  /** Number of pages captured. */
  pages: number;
  /** The extraction method actually used (`metrics.extraction.method`). */
  method: string;
  /** Number of pages that fell back to the Review Locator. */
  fallbacks: number;
}

/**
 * The `status_detail.viability` block: the stored Viability {@link Verdict}
 * (its `verdict` label, `reasons`, `warnings`, and `evidence`) with the
 * optional `actual` result appended once a version completes. The verdict
 * fields are reused verbatim from the Check client's {@link Verdict} so the
 * prediction renders identically to the URL-tab verdict card.
 */
export interface Viability extends Verdict {
  /** The actual processing result, once a version has completed. */
  actual?: ViabilityActual;
}

/**
 * Full record from `GET /datasets/{id}` (design "API endpoints").
 *
 * Extends the list {@link Dataset} with the detail-only fields the backend
 * `library.get_dataset` returns — `source_type`, `original_url`, `final_url`,
 * `page_title`, `metrics`, and the `versions` list — plus the `status_detail`
 * log and the optional upload `description`. `status_detail` and `description`
 * are optional so the type stays correct whether or not the detail endpoint
 * populates them yet.
 */
export interface DatasetDetail extends Dataset {
  source_type: SourceType;
  /** The original URL the analyst submitted (null for uploads). */
  original_url: string | null;
  /** The URL the request ultimately resolved to (null for uploads). */
  final_url: string | null;
  /** The captured page's `<title>`, when known. */
  page_title: string | null;
  /** Active-version metrics (review-analysis); null until a version lands. */
  metrics: Record<string, unknown> | null;
  /** Optional analyst-supplied description for an upload dataset. */
  description?: string | null;
  /** The append-only status event log + redirect/viability detail. */
  status_detail?: StatusDetail;
  versions: DatasetVersion[];
}

const BASE = "/api";

/** Resolve an API path to a same-origin absolute URL (mirrors `./ingest`). */
function url(path: string): string {
  const origin =
    typeof window !== "undefined" && window.location?.origin
      ? window.location.origin
      : "http://localhost";
  return `${origin}${BASE}${path}`;
}

/** Parse a `Retry-After` header value (seconds) into a number, or null. */
function parseRetryAfter(response: Response): number | null {
  const raw = response.headers.get("Retry-After");
  if (raw == null) return null;
  const seconds = Number.parseInt(raw, 10);
  return Number.isFinite(seconds) ? seconds : null;
}

/** Throw an {@link ApiError} describing a non-ok response. */
async function throwApiError(response: Response): Promise<never> {
  let code = "UNKNOWN";
  let message = response.statusText || `Request failed (${response.status})`;
  try {
    const body = (await response.json()) as {
      error?: { code?: string; message?: string };
    };
    if (body.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
    }
  } catch {
    // Non-JSON body; keep the status-line fallback.
  }
  throw new ApiError(response.status, code, message, parseRetryAfter(response));
}

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    await throwApiError(response);
  }
  return (await response.json()) as T;
}

/** Options for {@link listDatasets}. */
export interface ListDatasetsParams {
  /** Include archived datasets instead of the default active list. */
  archived?: boolean;
  /** Sort order (default `activity`). */
  sort?: DatasetSort;
  /** Case-insensitive name/URL filter text. */
  q?: string;
}

/** Envelope returned by `GET /datasets`: the rows under a `datasets` key. */
interface ListDatasetsResponse {
  datasets?: Dataset[];
}

/**
 * List tracked datasets (`GET /datasets?archived=&sort=&q=`).
 *
 * The backend applies the archived filter, sort, and text search, then wraps
 * the already-ordered, already-filtered rows in an envelope
 * `{"datasets": [...]}` (design Property 3). We unwrap that envelope here and
 * return the bare `Dataset[]` so callers and the `['datasets']` list cache keep
 * holding an array.
 */
export async function listDatasets(
  params: ListDatasetsParams = {},
): Promise<Dataset[]> {
  const search = new URLSearchParams();
  search.set("archived", params.archived ? "true" : "false");
  if (params.sort) search.set("sort", params.sort);
  if (params.q && params.q.trim().length > 0) search.set("q", params.q.trim());
  const response = await fetch(url(`/datasets?${search.toString()}`));
  const body = await parseJson<ListDatasetsResponse>(response);
  return body.datasets ?? [];
}

/** Fetch one dataset's full record (`GET /datasets/{id}`). */
export async function getDataset(id: string): Promise<DatasetDetail> {
  const response = await fetch(url(`/datasets/${encodeURIComponent(id)}`));
  return parseJson<DatasetDetail>(response);
}

/**
 * Response of `GET /datasets/{id}/snapshot-url` (design "API endpoints",
 * backend `summary.SnapshotUrl`).
 *
 * - `url`: a short-lived (5-minute) pre-signed GET URL for the active version's
 *   PNG snapshot (or version 1's while the first version is still processing).
 * - `expires_at`: ISO-8601 instant at which `url` stops working.
 * - `version`: the version whose snapshot `url` points at, so the client can
 *   invalidate `['snapshot', id]` when the active version changes.
 */
export interface SnapshotUrl {
  url: string;
  expires_at: string;
  version: number;
}

/**
 * Fetch a dataset's page-snapshot pre-signed URL
 * (`GET /datasets/{id}/snapshot-url`).
 *
 * Returns `{url, expires_at, version}` for a URL dataset. Upload datasets (and
 * unknown ids) return `404`, which raises an {@link ApiError} with status 404
 * — the {@link SnapshotCard} treats that as "no snapshot" (uploads have no page
 * snapshot) rather than an error to surface.
 */
export async function getSnapshotUrl(id: string): Promise<SnapshotUrl> {
  const response = await fetch(
    url(`/datasets/${encodeURIComponent(id)}/snapshot-url`),
  );
  return parseJson<SnapshotUrl>(response);
}

/**
 * One review in a `GET /datasets/{id}/reviews` page (Requirement 5.1).
 *
 * This mirrors exactly the review object the review-analysis metrics stage
 * writes into `reviews/v{n}.json` and the reviews endpoint returns verbatim
 * (`app/handlers/metrics.py::compute_metrics` `reviews_payload`): `id`, `text`,
 * `rating`, `date`, `author`, `title`, `sentiment`, and `source_page`. `rating`,
 * `date`, `author`, and `title` are nullable (not every review carries them, and
 * an upload may omit ratings entirely); `text` and `sentiment` are always
 * present. We do not invent fields — the table reads only what the producer
 * writes.
 */
export interface ReviewItem {
  /** Stable per-version id (`review_id(index)` in the metrics stage). */
  id: string;
  /** The review text, copied verbatim from the source page (never AI text). */
  text: string;
  /** Star rating 1–5, or null when the review has no rating. */
  rating: number | null;
  /** The review date (as stored), or null when unknown. */
  date: string | null;
  /** The review author, or null when unknown. */
  author: string | null;
  /** The review title/headline, or null when absent. */
  title: string | null;
  /** The sentiment label assigned by review-analysis. */
  sentiment: ReviewSentiment;
  /** The 1-based source page the review was captured from. */
  source_page?: number;
}

/** The three sentiment labels review-analysis assigns (backend `Sentiment`). */
export type ReviewSentiment = "positive" | "neutral" | "negative";

/**
 * Response of `GET /datasets/{id}/reviews` (design "API endpoints", backend
 * `summary.ReviewsPage`): the already-filtered, already-paginated page.
 *
 * - `items`: the reviews on this page (the server applied the rating/sentiment/
 *   text filters and the page slice).
 * - `total`: how many reviews matched the filters across all pages, so the
 *   client can render the pager.
 * - `page`: the 1-based page number these items came from.
 */
export interface ReviewsPage {
  items: ReviewItem[];
  total: number;
  page: number;
}

/**
 * Filter + pagination params for {@link listReviews} (design:
 * `?page&page_size&rating&sentiment&q`). All optional: an unset filter does not
 * constrain, matching the server, which treats a missing param as "any".
 */
export interface ListReviewsParams {
  /** 1-based page number (default 1 on the server). */
  page?: number;
  /** Items per page (server default 25, capped at 100). */
  page_size?: number;
  /** Exact star rating to filter on (1–5), or undefined for any. */
  rating?: number | null;
  /** Sentiment label to filter on, or undefined for any. */
  sentiment?: ReviewSentiment | null;
  /** Case-insensitive substring to search review text for. */
  q?: string;
}

/**
 * List a dataset's active-version reviews
 * (`GET /datasets/{id}/reviews?page&page_size&rating&sentiment&q`).
 *
 * Server-paginated and server-filtered: the page and filters go to the backend,
 * which reads the active-version `reviews/v{n}.json`, applies the rating,
 * sentiment, and text filters, and returns the single already-paginated page as
 * `{items, total, page}`. Only params that constrain are sent, so an empty
 * filter is omitted rather than sent blank (keeping the query string clean and
 * matching the server's "missing means any").
 */
export async function listReviews(
  id: string,
  params: ListReviewsParams = {},
): Promise<ReviewsPage> {
  const search = new URLSearchParams();
  if (params.page != null) search.set("page", String(params.page));
  if (params.page_size != null) search.set("page_size", String(params.page_size));
  if (params.rating != null) search.set("rating", String(params.rating));
  if (params.sentiment != null && params.sentiment.length > 0) {
    search.set("sentiment", params.sentiment);
  }
  if (params.q != null && params.q.trim().length > 0) {
    search.set("q", params.q.trim());
  }
  const query = search.toString();
  const suffix = query.length > 0 ? `?${query}` : "";
  const response = await fetch(
    url(`/datasets/${encodeURIComponent(id)}/reviews${suffix}`),
  );
  return parseJson<ReviewsPage>(response);
}

/** Rename a dataset (`PATCH /datasets/{id}`). */
export async function renameDataset(
  id: string,
  name: string,
): Promise<DatasetDetail> {
  const response = await fetch(url(`/datasets/${encodeURIComponent(id)}`), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  return parseJson<DatasetDetail>(response);
}

/** Archive a dataset (`POST /datasets/{id}/archive`). */
export async function archiveDataset(id: string): Promise<void> {
  const response = await fetch(url(`/datasets/${encodeURIComponent(id)}/archive`), {
    method: "POST",
  });
  if (!response.ok) await throwApiError(response);
}

/** Restore an archived dataset (`POST /datasets/{id}/restore`). */
export async function restoreDataset(id: string): Promise<void> {
  const response = await fetch(url(`/datasets/${encodeURIComponent(id)}/restore`), {
    method: "POST",
  });
  if (!response.ok) await throwApiError(response);
}

/** `202` body of `POST /datasets/{id}/refresh`. */
export interface RefreshResponse {
  check_id: string;
}

/**
 * Start a refresh for a URL dataset (`POST /datasets/{id}/refresh`).
 *
 * Returns the `{check_id}` of the refresh-origin Check. A `409`
 * `ALREADY_REFRESHING` raises an {@link ApiError} (the UI also disables the
 * action while a refresh is in flight). A `429` raises an {@link ApiError} with
 * `retryAfterSeconds`.
 */
export async function refreshDataset(id: string): Promise<RefreshResponse> {
  const response = await fetch(url(`/datasets/${encodeURIComponent(id)}/refresh`), {
    method: "POST",
  });
  return parseJson<RefreshResponse>(response);
}

/**
 * Confirm a `limited` refresh verdict (`POST /datasets/{id}/refresh/confirm`).
 *
 * Sends the `{check_id}` whose item is `awaiting_confirmation`; the backend
 * then runs `refresh_service.refresh(..., trigger="manual_refresh")`.
 */
export async function confirmRefresh(
  id: string,
  checkId: string,
): Promise<void> {
  const response = await fetch(
    url(`/datasets/${encodeURIComponent(id)}/refresh/confirm`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ check_id: checkId }),
    },
  );
  if (!response.ok) await throwApiError(response);
}
