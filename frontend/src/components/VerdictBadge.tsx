/**
 * VerdictBadge — the ✓ / ⚠ / ✕ verdict label.
 *
 * The design requires the verdict to read as text, not color alone, so the
 * badge always renders a word ("Will work" / "Limited" / "Won't work") next to
 * the glyph. The `data-verdict` attribute lets tests assert the verdict without
 * depending on the visible wording.
 */
import type { VerdictLabel } from "../api/ingest";

const LABELS: Record<VerdictLabel, { glyph: string; text: string }> = {
  will_work: { glyph: "✓", text: "Will work" },
  limited: { glyph: "⚠", text: "Limited" },
  wont_work: { glyph: "✕", text: "Won't work" },
};

export interface VerdictBadgeProps {
  verdict: VerdictLabel;
}

export default function VerdictBadge({ verdict }: VerdictBadgeProps) {
  const { glyph, text } = LABELS[verdict];
  return (
    <span className="verdict-badge" data-testid="verdict-badge" data-verdict={verdict}>
      <span aria-hidden="true">{glyph}</span> <span>{text}</span>
    </span>
  );
}
