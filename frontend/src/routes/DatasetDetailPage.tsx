/**
 * DatasetDetailPage — the `/datasets/{id}` route (ingestion-summary task 2.1).
 *
 * This is the shell that holds the Ingestion Summary and (later) the
 * guardrailed-chat panel. It owns the detail data fetch and the page's
 * responsive layout; the individual summary components are built in later tasks
 * (2.2–7) and drop into the clearly-marked placeholder regions here.
 *
 * Layout (design "Architecture"):
 *   - the header and readiness banner run across the full width;
 *   - below them two columns — left: metrics, rating chart, entity, themes;
 *     right: snapshot, completeness, prediction-versus-actual;
 *   - then collapsible timeline and reviews-table regions;
 *   - the chat panel sits at the bottom.
 * On narrow screens the two columns stack into one (the layout CSS owns the
 * breakpoint; the markup is a plain two-region grid).
 *
 * Data: the detail record comes from {@link useDataset}, keyed on
 * `['dataset', id]` — the same cache entry the real-time reducers patch, so the
 * live updates wired in task 6.2 land on what this page renders. The detail
 * endpoint (`GET /datasets/{id}`) and its client (`getDataset`) are owned by
 * `dataset-library`; this route reuses them rather than re-implementing.
 *
 * Not-found (design "Error Handling"): when the detail query returns 404 the
 * page shows "Dataset not found" with a link back to the Library instead of the
 * summary layout.
 *
 * Live states (task 6.3, Requirements 6.2 & 6.4): the page derives its live
 * display state with {@link detailPageStateFor} and renders accordingly —
 *   - **first-version processing** (no active version yet): the metric,
 *     snapshot, and reviews slots show {@link SlotSkeleton} placeholders, the
 *     {@link ProcessingNotice} shows the latest `last_message`, and the chat
 *     slot is marked disabled;
 *   - **refresh in progress** (`ready_refreshing`, an active version exists):
 *     everything stays usable and {@link ReadinessBanner} shows the refreshing
 *     note (task 4.3);
 *   - **failed**: {@link ProcessingNotice} shows the failure message and a
 *     Refresh action (reusing {@link useRefreshDataset}), plus "still showing
 *     v{n}" when an earlier version is still active.
 *
 * Chat slot: the guardrailed-chat {@link ChatPanel} mounts here (guardrailed-
 * chat task 9). This page owns the detail→availability mapping
 * ({@link chatAvailabilityFor}) and passes the derived {@link ChatAvailability}
 * (plus the active version for the VersionNote) into the panel, so the panel
 * stays a pure composition. The slot keeps publishing `data-chat-disabled` /
 * `aria-disabled` (the ingestion-summary contract other code and tests read),
 * which stays consistent with the availability: disabled exactly when there is
 * no active version to ground answers in. The panel's own real-time channel is
 * turned off here because this page already mounts {@link useRealtime}; that one
 * channel drives both the detail cache patch and the chat-history refetch that
 * makes refresh markers go live (realtimeReducers invalidates `['chat-history',
 * id]`), so a second channel would be redundant.
 */
import type { MouseEvent } from "react";

import { ApiError } from "../api/ingest";
import ChatPanel from "../components/ChatPanel";
import { chatAvailabilityFor } from "../components/chatAvailability";
import CompletenessPanel from "../components/CompletenessPanel";
import DatasetHeader from "../components/DatasetHeader";
import EntityCard from "../components/EntityCard";
import MetricsPanel from "../components/MetricsPanel";
import PredictionPanel from "../components/PredictionPanel";
import ProcessingNotice from "../components/ProcessingNotice";
import ProcessingTimeline from "../components/ProcessingTimeline";
import RatingDistributionChart from "../components/RatingDistributionChart";
import ReadinessBanner from "../components/ReadinessBanner";
import ReviewsTable from "../components/ReviewsTable";
import SlotSkeleton from "../components/SlotSkeleton";
import SnapshotCard from "../components/SnapshotCard";
import ThemesList from "../components/ThemesList";
import { detailPageStateFor } from "../components/detailPageState";
import { useActiveVersionRefetch } from "../hooks/useActiveVersionRefetch";
import { useDataset } from "../hooks/useDatasets";
import { useRealtime } from "../hooks/useRealtime";
import { LIBRARY_PATH } from "../routes";

export interface DatasetDetailPageProps {
  /** The dataset id from the `/datasets/{id}` path. */
  datasetId: string;
  /**
   * Navigate to a path. Defaults to the History API so the route needs no
   * mounted `<Router>` (consistent with {@link LibraryPage}).
   */
  onNavigate?: (path: string) => void;
  /**
   * Open the real-time channel for this page (default true). Set false in tests
   * that drive the cache directly and don't want a live WebSocket. Mirrors
   * {@link LibraryPage}'s `realtimeEnabled` flag.
   */
  realtimeEnabled?: boolean;
}

/** Default navigation: push onto the History API (no router dependency). */
function defaultNavigate(path: string): void {
  if (typeof window === "undefined") return;
  window.history.pushState(null, "", path);
}

/** True when the query error is a 404 (dataset does not exist). */
function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

export default function DatasetDetailPage({
  datasetId,
  onNavigate = defaultNavigate,
  realtimeEnabled = true,
}: DatasetDetailPageProps) {
  // Open the tab's single real-time channel so `dataset.status.changed` frames
  // for this dataset patch the `['dataset', id]` detail cache in place (the
  // dataset-library reducer, Requirement 6.3). The hook is idempotent per URL,
  // so mounting it here (the detail page is a standalone route) is safe even if
  // the Library also mounted it.
  useRealtime({ enabled: realtimeEnabled });

  const query = useDataset(datasetId);

  // When the live patch moves `active_version` to a new version, refetch the
  // detail and invalidate the reviews/snapshot queries so the new version's
  // data loads in place (Requirement 6.3; design "Live behavior"). This lives
  // in the ingestion-summary layer and observes the version the reducer wrote.
  useActiveVersionRefetch(datasetId, query.data?.active_version);

  const handleLibraryClick = (
    event: MouseEvent<HTMLAnchorElement>,
  ): void => {
    // Let modified clicks (new tab, etc.) fall through to the browser.
    if (event.defaultPrevented || event.metaKey || event.ctrlKey) return;
    event.preventDefault();
    onNavigate(LIBRARY_PATH);
  };

  // Not-found: show a clear message and a link back to the Library.
  if (query.isError && isNotFound(query.error)) {
    return (
      <main
        className="dataset-detail dataset-detail--not-found"
        data-testid="dataset-detail-page"
        aria-labelledby="dataset-not-found-heading"
      >
        <div className="dataset-detail__not-found" data-testid="dataset-not-found" role="alert">
          <h1 id="dataset-not-found-heading">Dataset not found</h1>
          <p>This dataset doesn&rsquo;t exist or has been removed.</p>
          <a
            href={LIBRARY_PATH}
            data-testid="not-found-library-link"
            onClick={handleLibraryClick}
          >
            Back to the Library
          </a>
        </div>
      </main>
    );
  }

  // Any other error: surface it (not a 404). Later tasks refine per-section
  // error handling; the shell just needs to not render the summary layout.
  if (query.isError) {
    return (
      <main
        className="dataset-detail dataset-detail--error"
        data-testid="dataset-detail-page"
      >
        <div className="dataset-detail__error" data-testid="dataset-detail-error" role="alert">
          <h1>Couldn&rsquo;t load this dataset</h1>
          <p>{query.error.message}</p>
          <button
            type="button"
            data-testid="dataset-detail-retry"
            onClick={() => void query.refetch()}
          >
            Try again
          </button>
          <a href={LIBRARY_PATH} onClick={handleLibraryClick}>
            Back to the Library
          </a>
        </div>
      </main>
    );
  }

  const isLoading = query.isLoading;
  const dataset = query.data;

  // The live-state derivation (task 6.3, Requirements 6.2 & 6.4): first-version
  // processing shows skeletons + a progress line and disables the chat; a
  // failed run shows the failure message + a Refresh action (and "still showing
  // v{n}" when an earlier version is active); an active/refreshing dataset is
  // fully usable. A pure helper keeps the branch logic testable in isolation.
  const pageState = dataset ? detailPageStateFor(dataset) : null;
  const showSkeletons = pageState?.showSkeletons ?? false;
  const chatDisabled = pageState?.chatDisabled ?? false;

  // The chat's availability, derived from the same detail record (guardrailed-
  // chat task 9). Drives the mounted ChatPanel's input + VersionNote. The slot's
  // `data-chat-disabled` stays the ingestion-summary signal (no active version →
  // disabled); the availability refines that into the five ChatInput states.
  const chatAvailability = dataset ? chatAvailabilityFor(dataset) : "no_active_version";

  return (
    <main
      className="dataset-detail"
      data-testid="dataset-detail-page"
      data-loading={isLoading ? "true" : "false"}
      aria-busy={isLoading}
    >
      {/* Back to the Library — a persistent way off the detail page. */}
      <a
        className="dataset-detail__back"
        data-testid="detail-back-link"
        href={LIBRARY_PATH}
        onClick={handleLibraryClick}
      >
        <span aria-hidden="true">←</span> Back to datasets
      </a>

      {/* Full-width header + readiness banner (DatasetHeader: task 2.2;
          ReadinessBanner: task 4.3). */}
      <div className="dataset-detail__top" data-testid="detail-top">
        <section
          className="dataset-detail__header"
          data-testid="slot-header"
          aria-label="Dataset header"
        >
          {/* DatasetHeader (task 2.2): rendered once the detail record loads. */}
          {dataset && <DatasetHeader dataset={dataset} />}
        </section>
        <section
          className="dataset-detail__readiness"
          data-testid="slot-readiness"
          aria-label="Readiness"
        >
          {/* ReadinessBanner (task 4.3): the active version's readiness level
              plus the refreshing / refresh-failed note (Requirement 4.5). */}
          {dataset && <ReadinessBanner dataset={dataset} />}
        </section>
        {/* Live progress / failure notice (task 6.3, Requirements 6.2 & 6.4):
            the latest progress message while the first version builds, or the
            failure message + Refresh action (and "still showing v{n}") on a
            failed run. Renders nothing when a version is active. */}
        {dataset && pageState && (
          <section
            className="dataset-detail__notice"
            data-testid="slot-notice"
            aria-label="Processing status"
          >
            <ProcessingNotice datasetId={datasetId} state={pageState} />
          </section>
        )}
      </div>

      {/* Two-column body; stacks to one column on narrow screens. */}
      <div className="dataset-detail__columns" data-testid="detail-columns">
        <div className="dataset-detail__column dataset-detail__column--left" data-testid="detail-column-left">
          <section className="dataset-detail__slot" data-testid="slot-metrics" aria-label="Metrics">
            {/* MetricsPanel (task 3.1): the active version's headline numbers.
                While the first version is still processing (no active version
                yet, Requirement 6.2) a skeleton stands in for the panel. */}
            {dataset &&
              (showSkeletons ? (
                <SlotSkeleton label="Metrics" testId="skeleton-metrics" lines={4} />
              ) : (
                <MetricsPanel metrics={dataset.metrics} />
              ))}
          </section>
          <section className="dataset-detail__slot" data-testid="slot-rating-chart" aria-label="Rating distribution">
            {/* RatingDistributionChart (task 3.2): bars + accessible table. */}
            {dataset &&
              (showSkeletons ? (
                <SlotSkeleton label="Rating distribution" testId="skeleton-rating-chart" />
              ) : (
                <RatingDistributionChart metrics={dataset.metrics} />
              ))}
          </section>
          <section className="dataset-detail__slot" data-testid="slot-entity" aria-label="Identified entity">
            {/* EntityCard (task 3.3): identified entity + low-confidence chip. */}
            {dataset &&
              (showSkeletons ? (
                <SlotSkeleton label="Identified entity" testId="skeleton-entity" />
              ) : (
                <EntityCard metrics={dataset.metrics} />
              ))}
          </section>
          <section className="dataset-detail__slot" data-testid="slot-themes" aria-label="Recurring themes">
            {/* ThemesList (task 3.3): up to 8 themes with count + sentiment lean. */}
            {dataset &&
              (showSkeletons ? (
                <SlotSkeleton label="Recurring themes" testId="skeleton-themes" />
              ) : (
                <ThemesList metrics={dataset.metrics} />
              ))}
          </section>
        </div>

        <div className="dataset-detail__column dataset-detail__column--right" data-testid="detail-column-right">
          <section className="dataset-detail__slot" data-testid="slot-snapshot" aria-label="Page snapshot">
            {/* SnapshotCard (task 4.1): the captured-page snapshot for a URL
                dataset, with a lightbox and an expired-URL refetch. While the
                first version is still processing a skeleton stands in. */}
            {dataset &&
              (showSkeletons ? (
                <SlotSkeleton label="Page snapshot" testId="skeleton-snapshot" />
              ) : (
                <SnapshotCard datasetId={datasetId} />
              ))}
          </section>
          <section className="dataset-detail__slot" data-testid="slot-completeness" aria-label="Completeness">
            {/* CompletenessPanel (task 4.2): pages captured, extracted-versus-
                reported bar, extraction method, skipped count, and warnings. */}
            {dataset &&
              (showSkeletons ? (
                <SlotSkeleton label="Completeness" testId="skeleton-completeness" />
              ) : (
                <CompletenessPanel
                  metrics={dataset.metrics}
                  sourceType={dataset.source_type}
                />
              ))}
          </section>
          <section className="dataset-detail__slot" data-testid="slot-prediction" aria-label="Predicted versus actual">
            {/* PredictionPanel (task 4.4): the Viability Check prediction next
                to the actual result, with the large-difference highlight
                (Requirement 7). Reads `status_detail.viability`; an upload has
                no viability block and renders the empty state. */}
            {dataset && (
              <PredictionPanel viability={dataset.status_detail?.viability} />
            )}
          </section>
        </div>
      </div>

      {/* Collapsible timeline + reviews table (tasks 6.1 and 5). */}
      <section className="dataset-detail__slot" data-testid="slot-timeline" aria-label="Processing timeline">
        {/* ProcessingTimeline (task 6.1): the collapsible `status_detail.events`
            log, newest first, with local-time timestamps (Requirement 6.1). */}
        {dataset && <ProcessingTimeline statusDetail={dataset.status_detail} />}
      </section>
      <section className="dataset-detail__slot" data-testid="slot-reviews" aria-label="Sample reviews">
        {/* ReviewsTable (task 5): the server-paginated, filterable table of the
            extracted reviews (Requirements 5.1, 5.2). While the first version is
            still processing there are no reviews yet, so a skeleton stands in. */}
        {dataset &&
          (showSkeletons ? (
            <SlotSkeleton label="Sample reviews" testId="skeleton-reviews" lines={5} />
          ) : (
            <ReviewsTable datasetId={datasetId} />
          ))}
      </section>

      {/* Chat panel at the bottom (guardrailed-chat task 9).

          The slot still publishes `data-chat-disabled` / `aria-disabled` (the
          ingestion-summary contract: disabled exactly when there is no active
          version to ground answers in — see {@link deriveDetailPageState}), and
          now also mounts the guardrailed-chat {@link ChatPanel}. The panel gets
          the detail-derived {@link ChatAvailability} and the active version for
          its VersionNote; its own real-time channel is disabled because this
          page already owns {@link useRealtime} (that one channel drives the
          chat-history refetch that makes refresh markers go live). The panel is
          mounted only once the detail record has loaded, so it has an id to ask
          against and never fires its history/suggestions queries for an unknown
          dataset. */}
      <section
        className="dataset-detail__slot dataset-detail__chat"
        data-testid="slot-chat"
        data-chat-disabled={chatDisabled ? "true" : "false"}
        aria-disabled={chatDisabled}
        aria-label="Ask about these reviews"
      >
        {dataset && (
          <ChatPanel
            datasetId={datasetId}
            availability={chatAvailability}
            activeVersion={dataset.active_version}
            realtimeEnabled={false}
          />
        )}
      </section>
    </main>
  );
}
