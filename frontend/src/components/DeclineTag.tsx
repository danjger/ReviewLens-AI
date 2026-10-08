/**
 * DeclineTag — the subtle "Outside dataset scope" tag shown on a declined
 * answer (guardrailed-chat task 6.2, Requirement 3.4).
 *
 * When an Exchange's `scope === "declined"` the assistant politely refused an
 * out-of-scope question (design "Frontend (ChatPanel)": "A declined answer
 * shows a subtle 'Outside dataset scope' tag"). This renders that marker next
 * to the answer. It is deliberately understated — the decline wording itself
 * lives in the answer text; this is only a small, non-preachy label
 * (Requirement 3.4's "polite and not preachy" tone).
 *
 * It is a plain inline label (no interaction), so it needs no extra ARIA beyond
 * its own readable text; the `data-testid` lets tests assert its presence
 * without matching on counts or dates.
 */
export interface DeclineTagProps {
  /** The decline category, when known (e.g. "world_knowledge"); shown as a
   *  machine-readable `data-*` only, never surfaced as wording here. */
  category?: string | null;
}

/** The subtle label wording (design "Frontend (ChatPanel)"). */
const DECLINE_LABEL = "Outside dataset scope";

export default function DeclineTag({ category = null }: DeclineTagProps) {
  return (
    <span
      className="decline-tag"
      data-testid="decline-tag"
      data-scope-category={category ?? undefined}
    >
      {DECLINE_LABEL}
    </span>
  );
}
