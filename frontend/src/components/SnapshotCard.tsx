/**
 * SnapshotCard — the captured-page snapshot on the detail page
 * (ingestion-summary task 4.1, Requirements 3.1, 3.2, 3.3).
 *
 * WHERE the dataset came from a URL, the card shows the PNG snapshot for the
 * active version (or version 1 while the first version is still processing),
 * loaded through a short-lived pre-signed URL (Requirement 3.1). Clicking the
 * snapshot opens it full size in a lightbox (Requirement 3.2). When the
 * snapshot cannot load, the card shows a placeholder with the reason
 * (Requirement 3.3).
 *
 * **Source of the URL.** The pre-signed URL comes from
 * `GET /datasets/{id}/snapshot-url` via {@link useSnapshotUrl}, keyed on
 * `['snapshot', id]` (the same key the detail page invalidates when the active
 * version changes). Upload datasets have no page snapshot: that endpoint
 * returns `404`, which arrives here as an {@link ApiError} with status 404 and
 * is treated as "no snapshot" — the card renders nothing rather than an error.
 *
 * **Expired-URL refetch (design "Error Handling": "one automatic refetch").**
 * The pre-signed URL is short-lived, so an `<img>` that was handed a URL which
 * has since expired fails to load. On the FIRST load failure the card refetches
 * the snapshot-url query exactly once to mint a fresh URL and retries the
 * image. If the image fails again (a genuinely missing/broken snapshot, not an
 * expiry), the card shows the failure placeholder (Requirement 3.3). The
 * one-shot guard is keyed to the current URL so a later version's fresh URL is
 * allowed its own single retry.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/ingest";
import { useSnapshotUrl } from "../hooks/useDatasets";

export interface SnapshotCardProps {
  /** The dataset id from the `/datasets/{id}` route. */
  datasetId: string;
}

/** True when the query error is a 404 (upload dataset: no page snapshot). */
function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

export default function SnapshotCard({ datasetId }: SnapshotCardProps) {
  const query = useSnapshotUrl(datasetId);
  const snapshotUrl = query.data?.url ?? null;
  const snapshotVersion = query.data?.version ?? null;

  // Lightbox open/close (Requirement 3.2).
  const [lightboxOpen, setLightboxOpen] = useState(false);
  // Set once the image has failed after its one allowed expired-URL refetch.
  const [failed, setFailed] = useState(false);

  // One-shot refetch guard, keyed to the snapshot VERSION (not the URL): an
  // expired URL earns exactly one refetch per version, so the refetched URL
  // failing again shows the placeholder rather than refetching forever. A
  // genuine active-version change mints a new version and resets the guard so
  // the new version's snapshot gets its own single retry.
  const retriedForVersion = useRef<number | null>(null);

  // A new version (active-version change) clears the one-shot guard and the
  // failed state so the fresh version's image gets its own chance + retry.
  useEffect(() => {
    if (snapshotVersion != null) {
      retriedForVersion.current = null;
      setFailed(false);
    }
  }, [snapshotVersion]);

  const handleImageError = useCallback(() => {
    if (snapshotUrl == null || snapshotVersion == null) return;
    if (retriedForVersion.current !== snapshotVersion) {
      // First failure for this version: assume the pre-signed URL expired and
      // refetch once to mint a fresh one.
      retriedForVersion.current = snapshotVersion;
      void query.refetch();
      return;
    }
    // Already spent the single refetch for this version and it still failed:
    // show the placeholder (Requirement 3.3).
    setFailed(true);
  }, [snapshotUrl, snapshotVersion, query]);

  const closeLightbox = useCallback(() => setLightboxOpen(false), []);

  // Upload dataset (or unknown id): the endpoint 404s. Uploads have no page
  // snapshot, so render nothing.
  if (query.isError && isNotFound(query.error)) {
    return null;
  }

  // Loading the pre-signed URL.
  if (query.isLoading) {
    return (
      <section
        className="snapshot-card snapshot-card--loading"
        data-testid="snapshot-card"
        data-state="loading"
        aria-label="Page snapshot"
      >
        <h2 className="snapshot-card__heading">Page snapshot</h2>
        <div
          className="snapshot-card__skeleton"
          data-testid="snapshot-loading"
          aria-hidden="true"
        />
      </section>
    );
  }

  // The snapshot-url request itself failed (not a 404), or the image failed to
  // load after its one allowed refetch: show the placeholder with the reason
  // (Requirement 3.3).
  if (failed || (query.isError && !isNotFound(query.error))) {
    const reason =
      query.isError && query.error instanceof Error
        ? query.error.message
        : "The page snapshot could not be loaded.";
    return (
      <section
        className="snapshot-card snapshot-card--failed"
        data-testid="snapshot-card"
        data-state="failed"
        aria-label="Page snapshot"
      >
        <h2 className="snapshot-card__heading">Page snapshot</h2>
        <div
          className="snapshot-card__placeholder"
          data-testid="snapshot-placeholder"
          role="status"
        >
          <p className="snapshot-card__placeholder-title">
            Snapshot unavailable
          </p>
          <p
            className="snapshot-card__placeholder-reason"
            data-testid="snapshot-failure-reason"
          >
            {reason}
          </p>
        </div>
      </section>
    );
  }

  // No URL yet (e.g. still fetching with no data); nothing to render.
  if (snapshotUrl == null) {
    return null;
  }

  return (
    <section
      className="snapshot-card"
      data-testid="snapshot-card"
      data-state="ready"
      aria-label="Page snapshot"
    >
      <h2 className="snapshot-card__heading">Page snapshot</h2>

      <button
        type="button"
        className="snapshot-card__thumb-button"
        data-testid="snapshot-open"
        onClick={() => setLightboxOpen(true)}
        aria-label="View the page snapshot full size"
      >
        <img
          className="snapshot-card__thumb"
          data-testid="snapshot-image"
          src={snapshotUrl}
          alt="Snapshot of the captured review page"
          onError={handleImageError}
        />
      </button>

      {lightboxOpen && (
        <div
          className="snapshot-card__lightbox"
          data-testid="snapshot-lightbox"
          role="dialog"
          aria-modal="true"
          aria-label="Page snapshot, full size"
          onClick={closeLightbox}
          onKeyDown={(event) => {
            if (event.key === "Escape") closeLightbox();
          }}
        >
          <button
            type="button"
            className="snapshot-card__lightbox-close"
            data-testid="snapshot-lightbox-close"
            onClick={(event) => {
              event.stopPropagation();
              closeLightbox();
            }}
            aria-label="Close the full-size snapshot"
          >
            Close
          </button>
          <img
            className="snapshot-card__lightbox-image"
            data-testid="snapshot-lightbox-image"
            src={snapshotUrl}
            alt="Snapshot of the captured review page, full size"
            onError={handleImageError}
            onClick={(event) => event.stopPropagation()}
          />
        </div>
      )}
    </section>
  );
}
