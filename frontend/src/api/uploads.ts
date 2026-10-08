/**
 * Typed client for the Upload (tabular ingestion) endpoints
 * (dataset-ingestion task 9.3).
 *
 * These functions mirror the dataset-ingestion design's "API endpoints" table
 * and the backend router in `app/ingestion/api.py` (the `uploads_router` /
 * `datasets_router`). The three-step upload flow is:
 *
 * 1. `POST /uploads` → {@link createUpload}: declare the file; get back an
 *    `upload_id`, a pre-signed `put_url`, and its `expires_at`.
 * 2. `PUT put_url` → {@link putFileToUrl}: upload the bytes straight to S3 with
 *    the signed `Content-Type`/`Content-Length` (the browser-direct upload that
 *    lets the file exceed the API's request-size cap — Requirement 7.1). This
 *    uses `XMLHttpRequest` because `fetch` cannot report upload progress.
 * 3. `POST /uploads/{upload_id}/preview` → {@link previewUpload}: detect the
 *    columns, suggested mapping, a few sample rows, and the keep rule
 *    (Requirements 7.2, 7.6). A `422` here means the staged object was deleted
 *    (invalid/over-limit/no text column) and the analyst must re-upload.
 * 4. `POST /datasets/upload` → {@link submitUpload}: submit the previewed upload
 *    with the confirmed mapping and a required name (Requirement 7.4).
 *
 * The error shape reuses {@link ApiError} from `./ingest`, so a non-2xx response
 * carries the backend `{ error: { code, message } }` envelope (and `429`
 * `Retry-After`) consistently with the Check client.
 */

import { ApiError } from "./ingest";

/** The canonical review fields a column can be mapped to (backend `TEXT` etc.). */
export type CanonicalField = "text" | "rating" | "date" | "author" | "title";

/** The keep-rule labels the preview reports (backend `upload_parser`). */
export type KeepRule = "most_recent_by_date" | "first_in_file";

/** `201` body of `POST /uploads` (backend `CreateUploadResponse`). */
export interface CreateUploadResponse {
  upload_id: string;
  put_url: string;
  expires_at: string;
}

/**
 * `200` body of `POST /uploads/{upload_id}/preview`
 * (backend `UploadPreview.to_dict`).
 *
 * - `columns`: the file's header names in file order.
 * - `suggested_mapping`: canonical field → detected header name. Always carries
 *   `text`; optional fields appear only when detected.
 * - `sample_rows`: a few parsed rows as `{ header → cell }` for display.
 * - `usable_rows`: rows with non-empty review text.
 * - `will_keep`: how many rows survive the `MAX_REVIEWS` cap
 *   (`min(usable_rows, MAX_REVIEWS)`).
 * - `keep_rule`: which rows are kept when over the limit (Requirement 7.6).
 */
export interface UploadPreview {
  columns: string[];
  suggested_mapping: Partial<Record<CanonicalField, string>>;
  sample_rows: Array<Record<string, string>>;
  usable_rows: number;
  will_keep: number;
  keep_rule: KeepRule;
}

/** The confirmed canonical-field → header-name mapping sent on submit. */
export type ColumnMapping = Partial<Record<CanonicalField, string>>;

/** Body of `POST /datasets/upload` (backend `SubmitUploadRequest`). */
export interface SubmitUploadInput {
  upload_id: string;
  name: string;
  mapping: ColumnMapping;
  description?: string;
}

/** `201` body of `POST /datasets/upload` (backend `SubmitUploadResponse`). */
export interface SubmitUploadResponse {
  id: string;
}

const BASE = "/api";

/**
 * Resolve an API path to a same-origin absolute request URL.
 *
 * Mirrors the helper in `./ingest`: Node's `fetch` (jsdom/MSW) cannot parse a
 * bare relative path, so an absolute URL built from `window.location.origin`
 * works for both the real app and tests.
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

/** Throw an {@link ApiError} describing a non-ok `fetch` response. */
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

/** Arguments for {@link createUpload}: the file the browser is about to send. */
export interface CreateUploadInput {
  filename: string;
  size_bytes: number;
  content_type: string;
}

/**
 * Declare an upload and get a pre-signed PUT URL (`POST /api/uploads`).
 *
 * The backend refuses an over-limit or wrong-extension file here (before any
 * URL is issued) with a `422`, which raises an {@link ApiError} carrying the
 * specific message. A `429` raises an {@link ApiError} with `retryAfterSeconds`.
 */
export async function createUpload(
  input: CreateUploadInput,
): Promise<CreateUploadResponse> {
  const response = await fetch(url("/uploads"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return parseJson<CreateUploadResponse>(response);
}

/** Progress callback for the direct S3 upload: `fraction` is 0..1. */
export type UploadProgress = (fraction: number) => void;

/**
 * PUT the file bytes directly to the pre-signed `put_url` with upload progress.
 *
 * Uses `XMLHttpRequest` rather than `fetch` because only XHR exposes an upload
 * progress event (`upload.onprogress`); the whole thing is wrapped in a Promise
 * so callers can `await` it. The `Content-Type` must match the value signed
 * into the URL by `POST /uploads`, and the browser sets `Content-Length` from
 * the `File` automatically, so the signed content-length guard is satisfied
 * (Requirement 7.1).
 *
 * A non-2xx S3 response rejects with an {@link ApiError} whose `code` is
 * `UPLOAD_FAILED` (S3 returns XML, not our JSON envelope, so there is no inner
 * code to surface). A network error or an abort rejects likewise.
 *
 * @param putUrl - the pre-signed URL from {@link createUpload}.
 * @param file - the file to upload (its `type` should equal the signed type).
 * @param onProgress - optional 0..1 progress callback.
 * @param xhrFactory - injection seam for tests (defaults to `XMLHttpRequest`).
 */
export function putFileToUrl(
  putUrl: string,
  file: File,
  onProgress?: UploadProgress,
  xhrFactory: () => XMLHttpRequest = () => new XMLHttpRequest(),
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const xhr = xhrFactory();
    xhr.open("PUT", putUrl, true);
    // Match the Content-Type signed into the pre-signed PUT.
    if (file.type) {
      xhr.setRequestHeader("Content-Type", file.type);
    }

    if (onProgress && xhr.upload) {
      xhr.upload.onprogress = (event: ProgressEvent) => {
        if (event.lengthComputable && event.total > 0) {
          onProgress(event.loaded / event.total);
        }
      };
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        if (onProgress) onProgress(1);
        resolve();
      } else {
        reject(
          new ApiError(
            xhr.status,
            "UPLOAD_FAILED",
            `Uploading the file failed (${xhr.status}). Please try again.`,
          ),
        );
      }
    };
    xhr.onerror = () =>
      reject(
        new ApiError(
          0,
          "UPLOAD_FAILED",
          "The file upload could not be completed. Check your connection and try again.",
        ),
      );
    xhr.onabort = () =>
      reject(new ApiError(0, "UPLOAD_ABORTED", "The file upload was cancelled."));

    xhr.send(file);
  });
}

/**
 * Preview a staged upload (`POST /api/uploads/{upload_id}/preview`).
 *
 * Returns the detected columns, suggested mapping, sample rows, and keep rule
 * (Requirements 7.2, 7.6). A `422` means the staged object was rejected and
 * deleted (over-limit, unparseable, or no usable text column — Requirement
 * 7.3); the caller surfaces the message and prompts a re-upload.
 */
export async function previewUpload(uploadId: string): Promise<UploadPreview> {
  const response = await fetch(
    url(`/uploads/${encodeURIComponent(uploadId)}/preview`),
    { method: "POST" },
  );
  return parseJson<UploadPreview>(response);
}

/**
 * Submit a previewed upload as a new dataset (`POST /api/datasets/upload`).
 *
 * Sends the `upload_id`, the required `name`, the confirmed `mapping`, and an
 * optional `description`; returns the new dataset's `id` (Requirement 7.4). A
 * `422` (missing text mapping, empty name, or a staged file that went invalid)
 * raises an {@link ApiError} with the backend message.
 */
export async function submitUpload(
  input: SubmitUploadInput,
): Promise<SubmitUploadResponse> {
  const response = await fetch(url("/datasets/upload"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return parseJson<SubmitUploadResponse>(response);
}
