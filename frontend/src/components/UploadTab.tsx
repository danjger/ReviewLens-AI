/**
 * UploadTab — the contents of the New Dataset panel's Upload tab
 * (dataset-ingestion task 9.3).
 *
 * The panel *layout* is owned by dataset-library; this is a self-contained
 * subtree it can mount beside the URL tab. It drives the design's Upload flow:
 *
 *   dropzone → pre-signed upload with a progress bar → preview with the
 *   over-limit note → column mapper → required name → submit
 *
 * composed from:
 * - {@link UploadDropzone}: pick a `.csv`/`.tsv` file (Requirement 7.1);
 * - {@link UploadProgressBar}: the direct-upload progress (Requirement 7.1);
 * - {@link UploadPreviewTable}: detected columns, sample rows, and the
 *   over-limit note (Requirements 7.2, 7.6);
 * - {@link UploadColumnMapper}: correct the mapping (Requirement 7.2);
 * - a required **name** input (and optional description) with submit gated on a
 *   non-empty name and a mapped text column (Requirement 7.4);
 * - {@link useUpload}: the orchestration and navigation to the new dataset's
 *   detail page on success (Requirements 7.4, 5.4).
 *
 * Selecting a file starts the pipeline immediately. Once the preview is ready
 * the analyst edits the mapping/name and submits. The mapping is seeded from
 * the parser's `suggested_mapping` and then owned here, so the preview
 * annotations and the submit gating always reflect what the analyst sees.
 */
import { useCallback, useEffect, useState } from "react";

import type { ColumnMapping } from "../api/uploads";
import { useUpload, type UseUploadOptions } from "../hooks/useUpload";
import RateLimitMessage from "./RateLimitMessage";
import UploadColumnMapper from "./UploadColumnMapper";
import UploadDropzone from "./UploadDropzone";
import UploadPreviewTable from "./UploadPreviewTable";
import UploadProgressBar from "./UploadProgressBar";

export interface UploadTabProps {
  /** Navigation / uploader overrides for the upload flow (tests inject these). */
  upload?: UseUploadOptions;
}

export default function UploadTab({ upload }: UploadTabProps) {
  const {
    phase,
    progress,
    preview,
    fileName,
    isBusy,
    rateLimit,
    needsReupload,
    errorMessage,
    start,
    submit,
    reset,
  } = useUpload(upload ?? {});

  const [mapping, setMapping] = useState<ColumnMapping>({});
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  // Seed the editable mapping from the parser's suggestion each time a new
  // preview arrives (a fresh file reseeds), then let the analyst edit it.
  useEffect(() => {
    if (preview) setMapping(preview.suggested_mapping);
  }, [preview]);

  const handleFile = useCallback(
    (file: File) => {
      // Starting a new upload clears the previous form state.
      setMapping({});
      setName("");
      setDescription("");
      start(file);
    },
    [start],
  );

  const showProgress = phase === "requesting" || phase === "uploading";
  const hasTextColumn = Boolean(mapping.text);
  const trimmedName = name.trim();
  const canSubmit =
    phase === "ready" && hasTextColumn && trimmedName.length > 0 && !isBusy;

  const handleSubmit = useCallback(() => {
    if (!canSubmit) return;
    submit({
      name: trimmedName,
      mapping,
      description: description.trim() || undefined,
    });
  }, [canSubmit, submit, trimmedName, mapping, description]);

  return (
    <section className="upload-tab" data-testid="upload-tab">
      <UploadDropzone
        onFile={handleFile}
        fileName={fileName}
        disabled={isBusy}
      />

      {showProgress && (
        <UploadProgressBar value={phase === "requesting" ? 0 : progress} />
      )}

      {rateLimit && <RateLimitMessage error={rateLimit} />}

      {needsReupload && (
        <div
          className="upload-tab__reupload"
          data-testid="upload-reupload-note"
          role="alert"
        >
          <p>{errorMessage ?? "The uploaded file is no longer available."}</p>
          <button
            type="button"
            data-testid="upload-reupload-button"
            onClick={reset}
          >
            Upload a different file
          </button>
        </div>
      )}

      {errorMessage && !needsReupload && (
        <p className="upload-tab__error" data-testid="upload-tab-error" role="alert">
          {errorMessage}
        </p>
      )}

      {preview && phase !== "done" && (
        <div className="upload-tab__form" data-testid="upload-form">
          <UploadPreviewTable preview={preview} mapping={mapping} />

          <UploadColumnMapper
            columns={preview.columns}
            mapping={mapping}
            onChange={setMapping}
            disabled={isBusy}
          />

          <div className="upload-tab__name">
            <label htmlFor="upload-name">
              Dataset name <span aria-hidden="true">*</span>
            </label>
            <input
              id="upload-name"
              type="text"
              data-testid="upload-name-input"
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
            />
          </div>

          <div className="upload-tab__description">
            <label htmlFor="upload-description">Source description (optional)</label>
            <textarea
              id="upload-description"
              data-testid="upload-description-input"
              rows={2}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>

          <button
            type="button"
            data-testid="upload-submit-button"
            disabled={!canSubmit}
            onClick={handleSubmit}
          >
            {phase === "submitting" ? "Submitting…" : "Create dataset"}
          </button>
        </div>
      )}
    </section>
  );
}
