/**
 * `useHtmlUpload` — orchestrate the HTML tab's upload → start-check pipeline
 * (dataset-ingestion task 21, Requirement 8.14).
 *
 * The HTML path mirrors the start of the Upload (CSV) flow but, instead of a
 * CSV preview, it starts a one-item Check Session on the uploaded saved page:
 *
 *   dropzone → pre-signed upload with a progress bar (reusing `POST /uploads`
 *   + the direct S3 PUT) → `POST /ingest/html-checks` to start the assessment
 *
 * Once the Check is started, the one item is driven by {@link useCheck} exactly
 * like a URL check (the design: "The single item polls / listens on the same
 * `check.updated` channel as a URL check"), so this hook owns only the two
 * machine-driven producer steps and hands the resulting `checkId` back to the
 * caller to persist (into `?check=`) and poll.
 *
 * The pre-signed PUT reuses {@link createUpload}/{@link putFileToUrl} from the
 * tabular path (the same mechanism, Requirement 8.1); the uploader is
 * injectable so tests can drive progress without a real `XMLHttpRequest`.
 */
import { useCallback, useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { ApiError, createHtmlCheck } from "../api/ingest";
import {
  createUpload,
  putFileToUrl,
  type UploadProgress,
} from "../api/uploads";

/** The HTML-upload producer lifecycle phase. */
export type HtmlUploadPhase =
  | "idle"
  | "requesting"
  | "uploading"
  | "starting"
  | "checking";

/** Inputs the `start` action needs: the file and the analyst's optional URL. */
export interface StartHtmlUploadInput {
  file: File;
  sourceUrl?: string;
}

/** What {@link useHtmlUpload} returns. */
export interface UseHtmlUploadResult {
  /** The current lifecycle phase. */
  phase: HtmlUploadPhase;
  /** Upload progress as a whole-number percentage (0..100). */
  progress: number;
  /** The name of the uploaded file (shown in place of a URL, Req 8.10). */
  fileName: string | null;
  /** True while any network step (upload or start-check) is in flight. */
  isBusy: boolean;
  /** A rate-limit error from `POST /ingest/html-checks`, if the last 429'd. */
  rateLimit: ApiError | null;
  /** A generic (non-rate-limit) error message, or null. */
  errorMessage: string | null;
  /** Begin the pipeline: request URL → PUT → start one-item Check. */
  start: (input: StartHtmlUploadInput) => void;
  /** Reset to the idle state (e.g. to upload a different file). */
  reset: () => void;
}

export interface UseHtmlUploadOptions {
  /**
   * Called with the new `check_id` once the one-item Check Session is created,
   * so the caller can persist it (into `?check=`) and drive {@link useCheck}.
   */
  onCheckCreated?: (checkId: string, itemId: string) => void;
  /**
   * Injectable uploader so tests can drive progress deterministically without a
   * real `XMLHttpRequest`. Defaults to {@link putFileToUrl}.
   */
  uploader?: (
    putUrl: string,
    file: File,
    onProgress?: UploadProgress,
  ) => Promise<void>;
}

/**
 * Drive one HTML-tab producer session.
 *
 * @param options - optional callback + uploader overrides.
 */
export function useHtmlUpload(
  options: UseHtmlUploadOptions = {},
): UseHtmlUploadResult {
  const uploader = options.uploader ?? putFileToUrl;
  const onCheckCreated = options.onCheckCreated;

  const [phase, setPhase] = useState<HtmlUploadPhase>("idle");
  const [progress, setProgress] = useState(0);
  const [fileName, setFileName] = useState<string | null>(null);

  const startMutation = useMutation<
    { check_id: string; item_id: string },
    Error,
    StartHtmlUploadInput
  >({
    mutationFn: async ({ file, sourceUrl }: StartHtmlUploadInput) => {
      setFileName(file.name);
      setProgress(0);

      setPhase("requesting");
      const created = await createUpload({
        filename: file.name,
        size_bytes: file.size,
        content_type: file.type || "text/html",
      });

      setPhase("uploading");
      await uploader(created.put_url, file, (fraction) => {
        setProgress(Math.round(fraction * 100));
      });
      setProgress(100);

      setPhase("starting");
      const check = await createHtmlCheck({
        upload_id: created.upload_id,
        source_url: sourceUrl,
      });
      return { check_id: check.check_id, item_id: check.item_id };
    },
    onSuccess: ({ check_id, item_id }) => {
      setPhase("checking");
      onCheckCreated?.(check_id, item_id);
    },
    onError: () => {
      // Keep the file name so the dropzone shows what failed; drop back to idle
      // so the analyst can retry or choose another file.
      setPhase("idle");
    },
  });

  const start = useCallback(
    (input: StartHtmlUploadInput) => {
      startMutation.reset();
      startMutation.mutate(input);
    },
    [startMutation],
  );

  const reset = useCallback(() => {
    startMutation.reset();
    setPhase("idle");
    setProgress(0);
    setFileName(null);
  }, [startMutation]);

  const lastError = startMutation.error ?? null;

  const rateLimit = useMemo(
    () =>
      lastError instanceof ApiError && lastError.isRateLimited
        ? lastError
        : null,
    [lastError],
  );

  const errorMessage = useMemo(() => {
    if (lastError == null) return null;
    if (rateLimit) return null; // surfaced via `rateLimit` separately
    return lastError.message;
  }, [lastError, rateLimit]);

  return {
    phase,
    progress,
    fileName,
    isBusy: startMutation.isPending,
    rateLimit,
    errorMessage,
    start,
    reset,
  };
}
