/**
 * ProcessingNotice — the live progress / failure banner on the detail page
 * (ingestion-summary task 6.3, Requirements 6.2 and 6.4).
 *
 * This renders the live-state message that sits with the summary while a
 * dataset is being processed or has failed, driven by the pure
 * {@link DetailPageState} the page derives:
 *
 * - **First-version processing** (Requirement 6.2) — a progress state with the
 *   latest `last_message`, shown while the first version is being built and the
 *   slots show skeletons.
 *
 * - **Failed** (Requirement 6.4) — the failure message plus a **Refresh**
 *   action to retry. The Refresh reuses the dataset-library
 *   {@link useRefreshDataset} mutation (this component does not re-implement
 *   it). WHEN an earlier version is still active, it also says the analysis is
 *   "still showing v{n}" (the page passes `stillShowingVersion`), so the
 *   analyst knows the data below is the previous good version.
 *
 * When the live state is `active` (a version is ready, possibly while a refresh
 * runs) there is nothing to announce here — the refreshing note is owned by
 * {@link ReadinessBanner} — so the component renders nothing.
 *
 * It is a thin view over {@link DetailPageState}; all of the branch logic lives
 * in {@link deriveDetailPageState} so it stays unit- and property-testable in
 * isolation.
 */
import { ApiError } from "../api/ingest";
import { useRefreshDataset } from "../hooks/useDatasets";
import type { DetailPageState } from "./detailPageState";

export interface ProcessingNoticeProps {
  /** The dataset id from the `/datasets/{id}` route (for the Refresh action). */
  datasetId: string;
  /** The derived live state (see {@link deriveDetailPageState}). */
  state: DetailPageState;
}

export default function ProcessingNotice({ datasetId, state }: ProcessingNoticeProps) {
  const refresh = useRefreshDataset();

  // Processing (first version): a progress line with the latest message.
  if (state.showProgress) {
    return (
      <section
        className="processing-notice processing-notice--processing"
        data-testid="processing-notice"
        data-phase={state.phase}
        role="status"
        aria-live="polite"
        aria-label="Processing progress"
      >
        <span className="processing-notice__spinner" aria-hidden="true" />
        <p className="processing-notice__message" data-testid="processing-message">
          {state.message ?? "Processing…"}
        </p>
      </section>
    );
  }

  // Failed: the failure message + a Refresh action, plus the "still showing
  // v{n}" line when an earlier version is still active (Requirement 6.4).
  if (state.showFailure) {
    // A 409 ALREADY_REFRESHING means a refresh is already in flight; surface it
    // without treating it as a hard failure of this action.
    const refreshError =
      refresh.isError && refresh.error instanceof ApiError
        ? refresh.error.message
        : refresh.isError && refresh.error instanceof Error
          ? refresh.error.message
          : null;

    return (
      <section
        className="processing-notice processing-notice--failed"
        data-testid="processing-notice"
        data-phase={state.phase}
        role="alert"
        aria-label="Processing failed"
      >
        <p className="processing-notice__message" data-testid="failure-message">
          {state.message ?? "Processing failed."}
        </p>

        {state.stillShowingVersion != null && (
          <p
            className="processing-notice__still-showing"
            data-testid="still-showing-version"
            data-version={state.stillShowingVersion}
          >
            Still showing v{state.stillShowingVersion}
          </p>
        )}

        <button
          type="button"
          className="processing-notice__refresh"
          data-testid="failure-refresh"
          onClick={() => refresh.mutate(datasetId)}
          disabled={refresh.isPending}
        >
          {refresh.isPending ? "Refreshing…" : "Refresh"}
        </button>

        {refreshError != null && (
          <p
            className="processing-notice__refresh-error"
            data-testid="failure-refresh-error"
            role="status"
          >
            {refreshError}
          </p>
        )}
      </section>
    );
  }

  // Active (a version is ready, possibly while a refresh runs): nothing to say.
  return null;
}
