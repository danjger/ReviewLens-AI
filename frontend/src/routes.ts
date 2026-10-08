/**
 * Shared route-path constants for the New Dataset / Add flow.
 *
 * The Library listing and the dataset detail page are owned by
 * `dataset-library`; this spec (dataset-ingestion) only needs to *navigate* to
 * them after an Add (Requirement 5.4). Keeping the paths here as constants means
 * dataset-library can align on the same values without this subtree hard-coding
 * string literals in several places.
 *
 * The functions build same-origin, root-relative paths. The Add flow drives
 * navigation through the History API (see {@link useAddItems}), consistent with
 * the way task 9.1's `useCheckParam` manipulates `window.location` directly
 * rather than depending on a mounted `<Router>`.
 */

/** The Library root — where multi-add stays after showing the summary. */
export const LIBRARY_PATH = "/";

/**
 * The detail page for a single dataset (`/datasets/{id}`), used to navigate the
 * analyst straight to a just-added dataset when exactly one was created or
 * refreshed (Requirement 5.4).
 */
export function datasetDetailPath(datasetId: string): string {
  return `/datasets/${encodeURIComponent(datasetId)}`;
}
