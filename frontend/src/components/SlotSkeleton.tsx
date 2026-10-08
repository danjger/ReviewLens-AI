/**
 * SlotSkeleton — a placeholder shown in a summary slot while the first version
 * is still processing and there is no data to render yet
 * (ingestion-summary task 6.3, Requirement 6.2).
 *
 * Requirement 6.2: "IF there is no active version yet, THEN the metric panels
 * SHALL show placeholders." This renders a labelled, accessible skeleton in
 * place of the real {@link MetricsPanel} / {@link SnapshotCard} /
 * {@link ReviewsTable} contents so the layout keeps its shape and the analyst
 * sees that data is on its way (rather than an empty gap or a stale zero).
 *
 * It is purely presentational: the detail page decides *when* to show it (see
 * {@link deriveDetailPageState}'s `showSkeletons`). The `aria-hidden` bars are
 * decorative; the accessible name comes from the `label` so screen-reader users
 * hear what the slot will hold.
 */
export interface SlotSkeletonProps {
  /** A short accessible label for what this slot will show (e.g. "Metrics"). */
  label: string;
  /** A stable test id for the slot's skeleton. */
  testId: string;
  /** How many shimmer bars to render (default 3). */
  lines?: number;
}

export default function SlotSkeleton({ label, testId, lines = 3 }: SlotSkeletonProps) {
  return (
    <div
      className="slot-skeleton"
      data-testid={testId}
      role="status"
      aria-busy="true"
      aria-label={`${label} — loading`}
    >
      <span className="visually-hidden">{label} is still processing…</span>
      {Array.from({ length: Math.max(1, lines) }, (_unused, index) => (
        <span
          key={index}
          className="slot-skeleton__bar"
          data-testid={`${testId}-bar`}
          aria-hidden="true"
        />
      ))}
    </div>
  );
}
