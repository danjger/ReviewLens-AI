/**
 * DatasetThumbnail — the small rendered-page thumbnail shown on each row of the
 * Tracked Datasets table (dataset-library task 10.2).
 *
 * Source-aware (never a bare broken image):
 *   - UPLOAD dataset: these have no page screenshot (Requirement 7.5), so show
 *     a CSV/file icon rather than a photo placeholder.
 *   - URL dataset WITH a `thumbnail_url`: show the presigned snapshot image,
 *     lazy-loaded and clipped (`object-fit: cover`). If it fails to load (e.g.
 *     the short-lived URL expired, or the object is absent), fall back to the
 *     neutral "no preview" placeholder instead of a broken image.
 *   - URL dataset WITHOUT a `thumbnail_url` (no successful version yet): the
 *     neutral placeholder.
 *
 * The choice is driven by `source_type` + presence of `thumbnail_url`, never by
 * sniffing the URL. Presentation only — no data fetching here; the row's
 * `thumbnail_url` is already a presigned GET from the list response.
 */
import { useState } from "react";

import type { SourceType } from "../api/datasets";

export interface DatasetThumbnailProps {
  sourceType: SourceType;
  thumbnailUrl: string | null;
  /** Dataset name, for an accessible alt text. */
  name: string;
}

/** A simple inline CSV/file glyph (no external asset dependency). */
function CsvGlyph() {
  return (
    <svg
      className="dataset-thumb__csv-icon"
      viewBox="0 0 24 24"
      width="24"
      height="24"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d="M6 2h8l4 4v16H6V2z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path
        d="M14 2v4h4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <text
        x="12"
        y="17"
        textAnchor="middle"
        fontSize="5"
        fontFamily="monospace"
        fill="currentColor"
      >
        CSV
      </text>
    </svg>
  );
}

export default function DatasetThumbnail({
  sourceType,
  thumbnailUrl,
  name,
}: DatasetThumbnailProps) {
  const [imageFailed, setImageFailed] = useState(false);

  // Upload datasets never have a page snapshot: show the CSV icon.
  if (sourceType === "upload") {
    return (
      <span
        className="dataset-thumb dataset-thumb--csv"
        data-testid="dataset-thumbnail"
        data-kind="csv"
        role="img"
        aria-label={`CSV upload: ${name}`}
      >
        <CsvGlyph />
      </span>
    );
  }

  // URL dataset with a usable snapshot URL that has not errored.
  if (thumbnailUrl && !imageFailed) {
    return (
      <span
        className="dataset-thumb dataset-thumb--image"
        data-testid="dataset-thumbnail"
        data-kind="image"
      >
        <img
          className="dataset-thumb__img"
          data-testid="dataset-thumbnail-img"
          src={thumbnailUrl}
          alt={`Rendered page for ${name}`}
          loading="lazy"
          onError={() => setImageFailed(true)}
        />
      </span>
    );
  }

  // No snapshot yet, or the image failed to load: neutral placeholder.
  return (
    <span
      className="dataset-thumb dataset-thumb--placeholder"
      data-testid="dataset-thumbnail"
      data-kind="placeholder"
      role="img"
      aria-label={`No preview for ${name}`}
    >
      <span className="dataset-thumb__placeholder-mark" aria-hidden="true" />
    </span>
  );
}
