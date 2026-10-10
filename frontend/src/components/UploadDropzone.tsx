/**
 * UploadDropzone — the file picker shared by the Upload (CSV) and HTML tabs
 * (dataset-ingestion tasks 9.3 and 21, Requirements 7.1 and 8.14).
 *
 * Accepts a single file, either by clicking to open the native file dialog or
 * by dragging a file onto the zone. The `accept` attribute hints the dialog
 * toward the allowed types, and this component also guards the extension
 * client-side so a wrong file is rejected with a message before any upload is
 * started (the backend applies the authoritative check on `POST /uploads`).
 *
 * The accepted extensions, prompt, and rejection message default to the Upload
 * tab's CSV/TSV values but are overridable so the HTML tab can accept saved
 * pages (`.html`/`.htm`/`.mhtml`) through the same component (Requirement 8.14).
 *
 * It is a thin, controlled input: it reports the chosen `File` upward via
 * `onFile` and renders the currently-selected file name (shown in place of a
 * URL — Requirements 7.5, 8.10). It owns no upload state; the hosting tab drives
 * the pipeline.
 */
import { useCallback, useRef, useState, type DragEvent } from "react";

/** Default extensions the dropzone accepts (Requirement 7.1, CSV/TSV). */
const DEFAULT_EXTENSIONS = [".csv", ".tsv"] as const;

/** Default rejection message when the chosen file is not an accepted type. */
const DEFAULT_REJECTED = "Only .csv and .tsv files can be uploaded.";

/** Default prompt text in the drop zone. */
const DEFAULT_PROMPT = "Drop a .csv or .tsv file here, or click to choose one";

export interface UploadDropzoneProps {
  /** Called with the chosen file once it passes the extension check. */
  onFile: (file: File) => void;
  /** The name of the already-chosen file, shown for confirmation (Req 7.5). */
  fileName?: string | null;
  /** Disable interaction while an upload/preview is in flight. */
  disabled?: boolean;
  /**
   * Accepted extensions (lowercase, with the leading dot). Defaults to CSV/TSV;
   * the HTML tab passes `.html`/`.htm`/`.mhtml` (Requirement 8.14).
   */
  extensions?: readonly string[];
  /** Prompt text shown in the zone. */
  prompt?: string;
  /** Message shown when the chosen file is not an accepted type. */
  rejectMessage?: string;
}

export default function UploadDropzone({
  onFile,
  fileName,
  disabled = false,
  extensions = DEFAULT_EXTENSIONS,
  prompt = DEFAULT_PROMPT,
  rejectMessage = DEFAULT_REJECTED,
}: UploadDropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [rejected, setRejected] = useState<string | null>(null);

  const acceptAttr = extensions.join(",");

  const handleFile = useCallback(
    (file: File | undefined) => {
      if (!file) return;
      const lower = file.name.toLowerCase();
      if (!extensions.some((ext) => lower.endsWith(ext))) {
        setRejected(rejectMessage);
        return;
      }
      setRejected(null);
      onFile(file);
    },
    [onFile, extensions, rejectMessage],
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
        <p className="upload-dropzone__prompt">{prompt}</p>
        {fileName && (
          <p className="upload-dropzone__filename" data-testid="upload-filename">
            {fileName}
          </p>
        )}
      </div>

      <input
        ref={inputRef}
        type="file"
        accept={acceptAttr}
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
