/**
 * `useUpload` — orchestrate the Upload tab's end-to-end flow
 * (dataset-ingestion task 9.3).
 *
 * The upload is a staged pipeline (design "Upload tab"): request a pre-signed
 * URL → PUT the bytes to S3 with a progress bar → preview the detected columns
 * → (the analyst edits the mapping and types a name) → submit as a new dataset.
 * This hook owns the first three machine-driven steps and exposes everything the
 * UI needs for the human steps:
 *
 * - {@link UseUploadResult.start} runs `createUpload` → `putFileToUrl` (progress)
 *   → `previewUpload` in sequence, surfacing `progress` (0..100) during the PUT
 *   and the resulting `preview` afterwards.
 * - {@link UseUploadResult.submit} runs `submitUpload` with the analyst's
 *   confirmed mapping + name, then navigates to the new dataset's detail page
 *   (Requirement 7.4 / 5.4), consistent with task 9.2's `useAddItems`.
 * - Errors are split so the UI can react: a `422` on preview/submit means the
 *   staged object was deleted and the analyst must re-upload
 *   (`needsReupload`), a `429` is a rate-limit, and anything else is a generic
 *   error.
 *
 * TanStack Query mutations back the two network-bound actions (`start`,
 * `submit`) so the UI gets `isPending` states for free; the live upload
 * progress is kept in component state and updated from the XHR callback. The
 * uploader and navigation are injectable so tests can drive progress and assert
 * navigation without a real `XMLHttpRequest` or `<Router>`.
 */
import { useCallback, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/ingest";
import {
  createUpload,
  previewUpload,
  putFileToUrl,
  submitUpload,
  type ColumnMapping,
  type UploadPreview,
  type UploadProgress,
} from "../api/uploads";
import { datasetDetailPath } from "../routes";
import { DATASETS_QUERY_KEY } from "./realtimeReducers";

/** The staged-upload lifecycle phase, for the UI to drive its view. */
export type UploadPhase =
  | "idle"
  | "requesting"
  | "uploading"
  | "previewing"
  | "ready"
  | "submitting"
  | "done";

/** Inputs the analyst supplies at submit time. */
export interface SubmitInput {
  name: string;
  mapping: ColumnMapping;
  description?: string;
}

/** What {@link useUpload} returns. */
export interface UseUploadResult {
  /** The current lifecycle phase. */
  phase: UploadPhase;
  /** Upload progress as a whole-number percentage (0..100). */
  progress: number;
  /** The preview once the staged file has been read, else null. */
  preview: UploadPreview | null;
  /** The name of the file being uploaded (shown in place of a URL, Req 7.5). */
  fileName: string | null;
  /** The id of the dataset created on submit, once done. */
  datasetId: string | null;
  /** True while any network step is in flight. */
  isBusy: boolean;
  /** A rate-limit error from `POST /uploads`, if the last start hit a 429. */
  rateLimit: ApiError | null;
  /** True when a 422 means the staged object is gone and a re-upload is needed. */
  needsReupload: boolean;
  /** A generic (non-rate-limit, non-reupload) error message, or null. */
  errorMessage: string | null;
  /** Begin the pipeline for `file`: request URL → PUT → preview. */
  start: (file: File) => void;
  /** Submit the previewed upload with the analyst's mapping + name. */
  submit: (input: SubmitInput) => void;
  /** Reset to the idle state (e.g. to upload a different file). */
  reset: () => void;
}

export interface UseUploadOptions {
  /**
   * Called to navigate to the new dataset's detail page after a successful
   * submit. Defaults to a History-API push to {@link datasetDetailPath}
   * (matching {@link useAddItems}). Override in tests or to use a router.
   */
  onNavigate?: (path: string) => void;
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

/** Default navigation: push the detail path onto the History API. */
function defaultNavigate(path: string): void {
  if (typeof window === "undefined") return;
  window.history.pushState(null, "", path);
}

/** True when `err` is a 422 (staged object deleted → re-upload needed). */
function isReuploadError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 422;
}

/**
 * Drive one Upload-tab session.
 *
 * @param options - optional navigation and uploader overrides.
 */
export function useUpload(options: UseUploadOptions = {}): UseUploadResult {
  const navigate = options.onNavigate ?? defaultNavigate;
  const uploader = options.uploader ?? putFileToUrl;
  const queryClient = useQueryClient();

  const [phase, setPhase] = useState<UploadPhase>("idle");
  const [progress, setProgress] = useState(0);
  const [preview, setPreview] = useState<UploadPreview | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [uploadId, setUploadId] = useState<string | null>(null);
  const [datasetId, setDatasetId] = useState<string | null>(null);

  // The full "request URL → PUT → preview" pipeline as one mutation, so the UI
  // sees a single pending/error state for the machine-driven steps.
  const startMutation = useMutation<UploadPreview, Error, File>({
    mutationFn: async (file: File) => {
      setFileName(file.name);
      setProgress(0);

      setPhase("requesting");
      const created = await createUpload({
        filename: file.name,
        size_bytes: file.size,
        content_type: file.type || "text/csv",
      });
      setUploadId(created.upload_id);

      setPhase("uploading");
      await uploader(created.put_url, file, (fraction) => {
        setProgress(Math.round(fraction * 100));
      });
      setProgress(100);

      setPhase("previewing");
      const result = await previewUpload(created.upload_id);
      return result;
    },
    onSuccess: (result) => {
      setPreview(result);
      setPhase("ready");
    },
    onError: () => {
      // Keep `uploadId`/`preview` cleared on a failed pipeline; the UI reads
      // `needsReupload`/`errorMessage` to decide what to show.
      setPhase("idle");
      setPreview(null);
    },
  });

  const submitMutation = useMutation<string, Error, SubmitInput>({
    mutationFn: async (input: SubmitInput) => {
      if (uploadId == null) {
        throw new ApiError(
          0,
          "NO_UPLOAD",
          "Upload a file before submitting.",
        );
      }
      setPhase("submitting");
      const response = await submitUpload({
        upload_id: uploadId,
        name: input.name,
        mapping: input.mapping,
        description: input.description,
      });
      return response.id;
    },
    onSuccess: (id) => {
      setDatasetId(id);
      setPhase("done");
      // The new dataset is born `requested`; refetch the Library list so a
      // "processing" row appears immediately rather than only after the next
      // poll/refresh (fixes "no indicator after uploading a CSV").
      void queryClient.invalidateQueries({ queryKey: [...DATASETS_QUERY_KEY] });
      navigate(datasetDetailPath(id));
    },
    onError: () => {
      // A 422 at submit means the staged file went invalid; fall back to
      // "ready" so the analyst still sees the form (needsReupload drives the
      // re-upload prompt).
      setPhase("ready");
    },
  });

  const start = useCallback(
    (file: File) => {
      setDatasetId(null);
      startMutation.reset();
      submitMutation.reset();
      startMutation.mutate(file);
    },
    [startMutation, submitMutation],
  );

  const submit = useCallback(
    (input: SubmitInput) => {
      submitMutation.mutate(input);
    },
    [submitMutation],
  );

  const reset = useCallback(() => {
    startMutation.reset();
    submitMutation.reset();
    setPhase("idle");
    setProgress(0);
    setPreview(null);
    setFileName(null);
    setUploadId(null);
    setDatasetId(null);
  }, [startMutation, submitMutation]);

  const lastError = startMutation.error ?? submitMutation.error ?? null;

  const rateLimit = useMemo(
    () =>
      lastError instanceof ApiError && lastError.isRateLimited
        ? lastError
        : null,
    [lastError],
  );

  const needsReupload = useMemo(() => isReuploadError(lastError), [lastError]);

  const errorMessage = useMemo(() => {
    if (lastError == null) return null;
    if (rateLimit) return null; // surfaced via `rateLimit` separately
    return lastError.message;
  }, [lastError, rateLimit]);

  return {
    phase,
    progress,
    preview,
    fileName,
    datasetId,
    isBusy: startMutation.isPending || submitMutation.isPending,
    rateLimit,
    needsReupload,
    errorMessage,
    start,
    submit,
    reset,
  };
}
