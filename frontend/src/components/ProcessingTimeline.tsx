/**
 * ProcessingTimeline — the processing event log on the detail page
 * (ingestion-summary task 6.1, Requirement 6.1).
 *
 * The analyst needs to see what the pipeline did and when, so this renders the
 * append-only `status_detail.events` log as a vertical timeline. Per the design
 * ("`ProcessingTimeline`: vertical list of `status_detail.events`, newest
 * first, with timestamps in local time") the newest event is shown first and
 * each timestamp is rendered in the viewer's local time.
 *
 * **Data source.** Each entry is a {@link StatusEvent} written by
 * `app/db/status.py` (`transition` / `log_event`): `status` (the lifecycle
 * status, or `null` for a progress-only event), `at` (ISO-8601 UTC), `message`,
 * and optional `data`. The field names mirror that producer exactly; this
 * component invents nothing. The incoming array is oldest-first as written, so
 * the timeline reverses it for display without mutating the source.
 *
 * **Timestamps.** The ISO-8601 `at` is rendered inside a `<time dateTime>`
 * element whose machine-readable value is the original ISO string and whose
 * visible text is the instant in the viewer's local time (via `toLocaleString`,
 * consistent with {@link DatasetHeader}). Tests assert on the `dateTime`
 * attribute, never on the locale-formatted text (testing.md: never depend on
 * dates).
 *
 * **Empty/missing (handled gracefully).** A missing `status_detail`, a missing
 * or non-array `events`, or an empty list all render a single "No processing
 * activity yet" empty state rather than an empty or broken list. Events with no
 * usable message are skipped; an event with an unparseable or missing `at`
 * still renders, just without a `<time>`.
 *
 * It sits in the collapsible timeline region of the detail page, so the markup
 * is a native `<details>`/`<summary>` disclosure (open by default) — no JS
 * state needed, and it stays keyboard-accessible for free.
 */
import type { StatusDetail, StatusEvent } from "../api/datasets";

export interface ProcessingTimelineProps {
  /**
   * The dataset's `status_detail` block (or undefined before it loads). The
   * timeline reads only `events`; every other key is ignored here.
   */
  statusDetail?: StatusDetail;
}

/** True when *value* is a non-empty string once trimmed. */
function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/** Format an ISO instant in the viewer's local time, or null when unparseable. */
function formatLocal(iso: string | null | undefined): string | null {
  if (!nonEmptyString(iso)) return null;
  const ms = new Date(iso).getTime();
  if (Number.isNaN(ms)) return null;
  return new Date(ms).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * Read the events array off the (optional) `status_detail`, keeping only the
 * entries that carry a usable message. Non-array/missing input yields `[]`.
 */
function readEvents(statusDetail: ProcessingTimelineProps["statusDetail"]): StatusEvent[] {
  const raw = statusDetail?.events;
  if (!Array.isArray(raw)) return [];
  return raw.filter((event): event is StatusEvent => {
    return event != null && typeof event === "object" && nonEmptyString(event.message);
  });
}

/** One timeline entry: a status chip (when present), the message, and a `<time>`. */
function TimelineEntry({ event, index }: { event: StatusEvent; index: number }) {
  const localTime = formatLocal(event.at);
  const status = nonEmptyString(event.status) ? event.status.trim() : null;

  return (
    <li
      className="processing-timeline__item"
      data-testid={`timeline-event-${index}`}
      data-status={status ?? undefined}
    >
      <div className="processing-timeline__marker" aria-hidden="true" />
      <div className="processing-timeline__body">
        {status != null && (
          <span
            className="processing-timeline__status"
            data-testid={`timeline-event-${index}-status`}
          >
            {status}
          </span>
        )}
        <span
          className="processing-timeline__message"
          data-testid={`timeline-event-${index}-message`}
        >
          {event.message}
        </span>
        {localTime != null && nonEmptyString(event.at) && (
          <time
            className="processing-timeline__time"
            data-testid={`timeline-event-${index}-time`}
            dateTime={event.at}
          >
            {localTime}
          </time>
        )}
      </div>
    </li>
  );
}

export default function ProcessingTimeline({ statusDetail }: ProcessingTimelineProps) {
  const events = readEvents(statusDetail);
  // Newest first for display, without mutating the source array.
  const ordered = [...events].reverse();

  return (
    <details className="processing-timeline" data-testid="processing-timeline" open>
      <summary className="processing-timeline__summary" data-testid="processing-timeline-summary">
        Processing timeline
      </summary>

      {ordered.length === 0 ? (
        <p className="processing-timeline__empty" data-testid="processing-timeline-empty">
          No processing activity yet.
        </p>
      ) : (
        <ol className="processing-timeline__list" data-testid="processing-timeline-list">
          {ordered.map((event, index) => (
            <TimelineEntry key={index} event={event} index={index} />
          ))}
        </ol>
      )}
    </details>
  );
}
