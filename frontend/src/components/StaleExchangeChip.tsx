/**
 * StaleExchangeChip — the "Based on earlier data" chip shown on an Exchange
 * whose data version is older than the dataset's current one (guardrailed-chat
 * task 6.6, Requirement 9.4).
 *
 * When the history layer flags an Exchange `is_stale` (its `data_version` is
 * older than the dataset's `active_version`), the row is de-emphasised and
 * carries this chip, labelled "Based on earlier data (v{n}, {date})" with an
 * explanatory tooltip ("the reviews have changed since") so the analyst knows
 * the answer may no longer apply (design "Frontend (ChatPanel)").
 *
 * **Local date (Requirement 9.4).** The chip shows the version and the date the
 * Exchange was answered against, formatted in the viewer's local time. The
 * machine-readable ISO instant is on a `<time dateTime>` element and the raw
 * version is a `data-*` attribute, so tests assert on structure, never on the
 * formatted date (testing.md).
 *
 * **Accessibility.** The tooltip text is wired to the chip with
 * `aria-describedby`, so a screen reader announces the explanation when the
 * chip is reached. The explanation is also mirrored into the native `title`
 * (visible on hover) and the tooltip element is always rendered (visually it
 * can be revealed on hover/focus via CSS), so the description is available to
 * keyboard and pointer users alike rather than being hover-only.
 */
import { useId } from "react";

export interface StaleExchangeChipProps {
  /** The (older) data version this Exchange was answered against. */
  version: number;
  /** ISO-8601 time the Exchange was asked/answered (shown as a local date). */
  askedAt: string | null | undefined;
}

/** The tooltip explanation (design / Requirement 9.4). */
const TOOLTIP_TEXT = "The reviews have changed since this answer was given.";

/** Format an ISO timestamp as a readable local date, or `null`. */
function formatDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime();
  if (Number.isNaN(ms)) return null;
  return new Date(ms).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export default function StaleExchangeChip({ version, askedAt }: StaleExchangeChipProps) {
  const tooltipId = useId();
  const dateText = formatDate(askedAt);

  return (
    <span
      className="stale-chip"
      data-testid="stale-chip"
      data-version={version}
      aria-describedby={tooltipId}
      title={TOOLTIP_TEXT}
    >
      <span className="stale-chip__label" data-testid="stale-chip-label">
        Based on earlier data (v{version}
        {dateText != null && askedAt != null ? (
          <>
            ,{" "}
            <time dateTime={askedAt} data-testid="stale-chip-date">
              {dateText}
            </time>
          </>
        ) : null}
        )
      </span>
      <span
        className="stale-chip__tooltip"
        data-testid="stale-chip-tooltip"
        id={tooltipId}
        role="tooltip"
      >
        {TOOLTIP_TEXT}
      </span>
    </span>
  );
}
