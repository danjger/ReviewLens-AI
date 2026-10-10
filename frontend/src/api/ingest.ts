/**
 * Typed client for the Check (URL ingestion) endpoints.
 *
 * These functions mirror the dataset-ingestion design's "API endpoints" table
 * and the backend router in `app/ingestion/api.py`. The URL tab (task 9.1) uses
 * `createCheck`, `getCheck`, and `retryCheckItem`; `addCheckItems` is listed
 * here for completeness and is driven by the Add flow (task 9.2).
 *
 * Error handling: a non-2xx response is turned into an {@link ApiError} that
 * carries the parsed `{ error: { code, message } }` envelope and, for `429`
 * responses, the numeric `Retry-After` seconds so the panel can say when checks
 * can resume (Requirement 1.6).
 */

/** The three viability verdict labels (backend `VerdictLabel`). */
export type VerdictLabel = "will_work" | "limited" | "wont_work";

/** The lifecycle state of a single check item (backend `ItemState`). */
export type ItemState =
  | "pending"
  | "checking"
  | "done"
  | "invalid"
  | "duplicate_in_batch"
  | "error"
  | "awaiting_confirmation"
  | "applied";

/** One sample review read from the page (never AI-generated). */
export interface Sample {
  text: string;
  rating: number | null;
  date: string | null;
}

/** The structured evidence behind a verdict (backend `Evidence.to_dict`). */
export interface Evidence {
  reviews_verified: number;
  reviews_rejected: number;
  method: string | null;
  pagination: boolean;
  reported_total: number | null;
  blocker: string | null;
  locator_confidence: string | null;
  page_title: string;
  main_status: number | null;
  samples: Sample[];
}

/** A verdict for one URL (backend `Verdict.to_dict`). */
export interface Verdict {
  verdict: VerdictLabel;
  reasons: string[];
  warnings: string[];
  evidence: Evidence;
}

/** The existing-dataset match recorded when a URL is already tracked. */
export interface ExistingDataset {
  id: string;
  name: string;
  archived: boolean;
  status: string;
}

/** One redirect/client-redirect hop recorded during the probe/capture. */
export interface Hop {
  url: string;
  status: number;
  timestamp?: string;
  kind?: string;
}

/**
 * One check item as returned by `GET /ingest/checks/{id}` (the polling shape,
 * backend `_item_to_view`). `verdict` and `existing_dataset` are populated once
 * the item reaches a terminal state.
 */
export interface CheckItem {
  item_id: string;
  input: string;
  normalized: string | null;
  final_url: string | null;
  state: ItemState;
  hops: Hop[];
  verdict: Verdict | null;
  existing_dataset: ExistingDataset | null;
  /** Set on the initial POST response for invalid/duplicate lines. */
  message?: string | null;
}

/** `202` body of `POST /ingest/checks` (backend `CreateCheckResponse`). */
export interface CreateCheckResponse {
  check_id: string;
  items: CheckItem[];
}

/** Body of `POST /ingest/html-checks` (backend `CreateHtmlCheckRequest`). */
export interface CreateHtmlCheckInput {
  upload_id: string;
  source_url?: string;
}

/** `202` body of `POST /ingest/html-checks` (backend `CreateHtmlCheckResponse`). */
export interface CreateHtmlCheckResponse {
  check_id: string;
  item_id: string;
  state: ItemState;
}

/** The polling session shape (`GET /ingest/checks/{id}`). */
export interface CheckSession {
  check_id: string;
  created_at: string;
  origin: string;
  items: CheckItem[];
}

/** One Add outcome (backend `AddResult.to_dict`). */
export type AddOutcome =
  | "created"
  | "refreshed"
  | "restored_and_refreshed"
  | "already_refreshing"
  | "refused_wont_work"
  | "needs_confirmation"
  | "expired";

/** One row of the `POST /ingest/checks/{id}/add` response. */
export interface AddResult {
  item_id: string;
  outcome: AddOutcome;
  dataset_id: string | null;
  message: string | null;
}

/** The error envelope every endpoint returns on failure. */
export interface ApiErrorBody {
  error: { code: string; message: string };
}

/**
 * An error thrown for any non-2xx response. Carries the HTTP status, the
 * backend error `code`/`message`, and (for `429`) the `Retry-After` seconds.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfterSeconds: number | null;

  constructor(
    status: number,
    code: string,
    message: string,
    retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  /** True when the request was rate-limited (Requirement 1.6). */
  get isRateLimited(): boolean {
    return this.status === 429;
  }
}

const BASE = "/api";

/**
 * Resolve an API path to a request URL.
 *
 * In the browser the app is served from the same origin as the API, so a
 * same-origin absolute URL built from `window.location.origin` works for both
 * the real app and `fetch`-based tests (Node's `fetch` cannot parse a bare
 * relative path, so an absolute URL is required under jsdom/MSW).
 */
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
    const body = (await response.json()) as Partial<ApiErrorBody>;
    if (body.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
    }
  } catch {
    // Non-JSON body (e.g. a proxy error); keep the status-line fallback.
  }
  throw new ApiError(response.status, code, message, parseRetryAfter(response));
}

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    await throwApiError(response);
  }
  return (await response.json()) as T;
}

/**
 * Create a Check Session for up to 10 URLs.
 *
 * `POST /api/ingest/checks`. Returns the `check_id` and each line's initial
 * state (`pending` / `invalid` / `duplicate_in_batch`). A `429` raises an
 * {@link ApiError} carrying `retryAfterSeconds` (Requirement 1.6).
 */
export async function createCheck(urls: string[]): Promise<CreateCheckResponse> {
  const response = await fetch(url("/ingest/checks"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ urls }),
  });
  return parseJson<CreateCheckResponse>(response);
}

/**
 * Start a one-item Check from an already-staged uploaded saved page (HTML tab).
 *
 * `POST /api/ingest/html-checks`. The analyst has already uploaded the saved
 * HTML through `POST /uploads`; this starts the viability assessment on it,
 * returning the new `check_id`/`item_id` and the item's initial `state`
 * (Requirement 8.1, 8.3). A `422` means the staged object is missing or the
 * optional `source_url` is malformed; a `429` raises an {@link ApiError}
 * carrying `retryAfterSeconds` (Requirement 8.7).
 */
export async function createHtmlCheck(
  input: CreateHtmlCheckInput,
): Promise<CreateHtmlCheckResponse> {
  const response = await fetch(url("/ingest/html-checks"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return parseJson<CreateHtmlCheckResponse>(response);
}

/**
 * Fetch a Check Session for polling.
 *
 * `GET /api/ingest/checks/{checkId}`. A `404` (expired session) raises an
 * {@link ApiError} with status 404 so the caller can prompt a re-check.
 */
export async function getCheck(checkId: string): Promise<CheckSession> {
  const response = await fetch(url(`/ingest/checks/${encodeURIComponent(checkId)}`));
  return parseJson<CheckSession>(response);
}

/**
 * Re-queue a single item that ended in `error` or timed out (Requirement 3.10).
 *
 * `POST /api/ingest/checks/{checkId}/items/{itemId}/retry`. A `429` raises an
 * {@link ApiError} carrying `retryAfterSeconds`.
 */
export async function retryCheckItem(
  checkId: string,
  itemId: string,
): Promise<void> {
  const response = await fetch(
    url(
      `/ingest/checks/${encodeURIComponent(checkId)}/items/${encodeURIComponent(itemId)}/retry`,
    ),
    { method: "POST" },
  );
  if (!response.ok) {
    await throwApiError(response);
  }
}

/**
 * One item in an Add request.
 *
 * For a URL item only `item_id` and the optional `confirm_limited` are read.
 * For an HTML-upload item (dataset-ingestion task 20/21) the body additionally
 * carries the required `name`, the optional analyst-supplied `source_url`, and
 * an optional `description` — these three fields are ignored for URL items
 * (design "API endpoints"; Requirement 8.8).
 */
export interface AddItemInput {
  item_id: string;
  confirm_limited?: boolean;
  name?: string;
  source_url?: string;
  description?: string;
}

/**
 * Add the chosen Check items (task 9.2 drives this; typed here for the client).
 *
 * `POST /api/ingest/checks/{checkId}/add`. Always returns `200` with per-item
 * outcomes (an expired session yields `expired` outcomes rather than a 404).
 */
export async function addCheckItems(
  checkId: string,
  items: AddItemInput[],
): Promise<AddResult[]> {
  const response = await fetch(url(`/ingest/checks/${encodeURIComponent(checkId)}/add`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  });
  const body = await parseJson<{ results: AddResult[] }>(response);
  return body.results;
}

/** Item states that will not change again (polling can stop). */
const TERMINAL_STATES: ReadonlySet<ItemState> = new Set<ItemState>([
  "done",
  "invalid",
  "duplicate_in_batch",
  "error",
  "awaiting_confirmation",
  "applied",
]);

/** True when an item has reached a state the worker won't advance further. */
export function isTerminal(item: CheckItem): boolean {
  return TERMINAL_STATES.has(item.state);
}
