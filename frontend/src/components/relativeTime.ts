/**
 * relativeTime — format an ISO timestamp as a short relative string
 * ("2h ago", "3d ago") for the Tracked Datasets row "last refreshed" column
 * (dataset-library task 6.3).
 *
 * Kept pure and separate so the row can render a human time without pulling in
 * a date library, and so tests can target the value by testid rather than
 * asserting on the formatted date text (testing.md: never assert on dates).
 */

/** Format `iso` relative to `now` (default: the current time). */
export function relativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diffMs = now.getTime() - then;
  const seconds = Math.round(diffMs / 1000);

  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo ago`;
  const years = Math.round(months / 12);
  return `${years}y ago`;
}
