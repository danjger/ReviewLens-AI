/**
 * TrackedNote — the "Already tracked" note shown when a checked URL matches an
 * existing dataset (Requirement 6.3).
 *
 * Two variants:
 * - Normal: "Already tracked as '{name}' — adding will refresh it," with a link
 *   to the dataset's detail page.
 * - Tracked `wont_work`: the page can't be read right now and the existing data
 *   is unchanged (still links to the dataset so the analyst can open it).
 *
 * The dataset link points at the Library detail route (`/datasets/{id}`), which
 * dataset-library owns; using an anchor keeps this component free of a router
 * dependency so it can be mounted anywhere.
 */
import type { ExistingDataset, VerdictLabel } from "../api/ingest";

export interface TrackedNoteProps {
  existing: ExistingDataset;
  verdict: VerdictLabel;
}

export default function TrackedNote({ existing, verdict }: TrackedNoteProps) {
  const href = `/datasets/${encodeURIComponent(existing.id)}`;
  const isWontWork = verdict === "wont_work";

  return (
    <p
      className="tracked-note"
      data-testid="tracked-note"
      data-variant={isWontWork ? "wont_work" : "refresh"}
    >
      {isWontWork ? (
        <>
          Already tracked as{" "}
          <a href={href} data-testid="tracked-link">
            {existing.name}
          </a>
          {" "}— the page can&apos;t be read right now; existing data unchanged.
        </>
      ) : (
        <>
          Already tracked as{" "}
          <a href={href} data-testid="tracked-link">
            {existing.name}
          </a>
          {" "}— adding will refresh it.
        </>
      )}
    </p>
  );
}
