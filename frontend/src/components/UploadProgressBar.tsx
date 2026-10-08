/**
 * UploadProgressBar — the direct-upload progress indicator for the Upload tab
 * (dataset-ingestion task 9.3, Requirement 7.1).
 *
 * While the browser PUTs the file straight to S3, this shows how far the upload
 * has got. The value is a whole-number percentage (0..100) driven by the XHR
 * upload-progress callback in {@link useUpload}. It renders a native
 * `<progress>` so assistive technology announces the value, and exposes the
 * numeric value on a `data-value` attribute so tests can assert progress
 * without depending on rendered text (testing.md: data-testid selectors, no
 * count-based text).
 */

export interface UploadProgressBarProps {
  /** The current upload percentage (0..100). */
  value: number;
  /** An accessible label for the progress element. */
  label?: string;
}

export default function UploadProgressBar({
  value,
  label = "Uploading file",
}: UploadProgressBarProps) {
  const clamped = Math.max(0, Math.min(100, Math.round(value)));
  return (
    <div
      className="upload-progress"
      data-testid="upload-progress"
      data-value={clamped}
    >
      <progress
        className="upload-progress__bar"
        max={100}
        value={clamped}
        aria-label={label}
      />
    </div>
  );
}
