/**
 * UploadDropzone — the file picker for the Upload tab (dataset-ingestion task
 * 9.3, Requirement 7.1).
 *
 * Accepts a single `.csv` or `.tsv` file, either by clicking to open the native
 * file dialog or by dragging a file onto the zone. The `accept` attribute hints
 * the dialog toward CSV/TSV, and this component also guards the extension
 * client-side so a wrong file is rejected with a message before any upload is
 * started (the backend applies the authoritative check on `POST /uploads`).
 *
 * It is a thin, controlled input: it reports the chosen `File` upward via
 * `onFile` and renders the currently-selected file name (shown in place of a
 * URL — Requirement 7.5). It owns no upload state; `UploadTab` drives the
 * pipeline.
 */
import { useCallback, useRef, useState, type DragEvent } from "react";

/** Extensions the dropzone accepts (Requirement 7.1). */
const ACCEPTED_EXTENSIONS = [".csv", ".tsv"] as const;

/** The `accept` attribute value for the native file dialog. */
const ACCEPT_ATTR = ACCEPTED_EXTENSIONS.join(",");

export interface UploadDropzoneProps {
  /** Called with the chosen file once it passes the extension check. */
  onFile: (file: File) => void;
  /** The name of the already-chosen file, shown for confirmation (Req 7.5). */
  fileName?: string | null;
  /** Disable interaction while an upload/preview is in flight. */
  disabled?: boolean;
}

/** True when `name` ends in an accepted extension (case-insensitive). */
function hasAcceptedExtension(name: string): boolean {
  const lower = name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

export default function UploadDropzone({
  onFile,
  fileName,
  disabled = false,
}: UploadDropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [rejected, setRejected] = useState<string | null>(null);

  const handleFile = useCallback(
    (file: File | undefined) => {
      if (!file) return;
      if (!hasAcceptedExtension(file.name)) {
        setRejected("Only .csv and .tsv files can be uploaded.");
        return;
      }
      setRejected(null);
      onFile(file);
    },
    [onFile],
  );

  const openDialog = useCallback(() => {
    if (disabled) return;
    inputRef.current?.click();
  }, [disabled]);

  const handleDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragOver(false);
      if (disabled) return;
      handleFile(event.dataTransfer.files?.[0]);
    },
    [disabled, handleFile],
  );

  const handleDragOver = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      if (!disabled) setDragOver(true);
    },
    [disabled],
  );

  const handleDragLeave = useCallback(() => setDragOver(false), []);

  return (
    <div className="upload-dropzone" data-testid="upload-dropzone">
      <div
        className={`upload-dropzone__zone${dragOver ? " upload-dropzone__zone--over" : ""}`}
        data-testid="upload-dropzone-zone"
        data-drag-over={dragOver}
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled}
        onClick={openDialog}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openDialog();
          }
        }}
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
      >
        <p className="upload-dropzone__prompt">
          Drop a .csv or .tsv file here, or click to choose one
        </p>
        {fileName && (
          <p className="upload-dropzone__filename" data-testid="upload-filename">
            {fileName}
          </p>
        )}
      </div>

      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT_ATTR}
        data-testid="upload-file-input"
        className="upload-dropzone__input"
        disabled={disabled}
        onChange={(event) => {
          handleFile(event.target.files?.[0]);
          // Clear the value so choosing the same file again re-fires onChange.
          event.target.value = "";
        }}
      />

      {rejected && (
        <p
          className="upload-dropzone__error"
          data-testid="upload-dropzone-error"
          role="alert"
        >
          {rejected}
        </p>
      )}
    </div>
  );
}
