/**
 * DatasetHeader — the dataset header on the detail page (ingestion-summary
 * task 2.2, Requirements 1.1–1.4).
 *
 * Shows which dataset the analyst is looking at and where it came from, so they
 * can be sure they're analysing the right source:
 *
 * - **Inline rename (Req 1.1).** The name is an editable field. Clicking "Rename"
 *   (or the name) opens an input; Save issues a PATCH through
 *   {@link useRenameDataset} (which reuses the dataset-library `renameDataset`
 *   client and patches the `['dataset', id]` cache). Escape / Cancel discards.
 * - **Source (Reqs 1.2, 1.3).** For a URL dataset the original URL is the main
 *   link; when the final URL differs it adds a "Resolved to: {final URL}" line
 *   and an expandable list of the redirect hops from `status_detail.redirects`.
 *   For an upload dataset it shows the file name and optional description in
 *   place of the URLs.
 * - **Meta (Req 1.4).** Platform, the {@link StatusBadge} for `display_state`
 *   (the same badge the Library uses), the request date, the last-updated date,
 *   and a "Data as of {completed_at} · v{active_version}" line for the version
 *   being shown.
 *
 * Dates/counts are rendered with dedicated testids and (for machine-readable
 * dates) a `<time dateTime>` element, so tests target structure rather than the
 * formatted text (testing.md: never assert on text with counts/dates).
 */
import { useEffect, useRef, useState } from "react";

import type { DatasetDetail } from "../api/datasets";
import type { Hop } from "../api/ingest";
import { useRenameDataset } from "../hooks/useDatasets";
import StatusBadge from "./StatusBadge";

export interface DatasetHeaderProps {
  dataset: DatasetDetail;
}

/** True when the dataset's reviews came from an uploaded file. */
function isUpload(dataset: DatasetDetail): boolean {
  return dataset.source_type === "upload";
}

/**
 * The active version's `completed_at`, i.e. when the shown data was captured.
 *
 * Prefers the matching `versions` row; falls back to the row-level
 * `last_refreshed_at`, which the Library derives as the active version's
 * `completed_at` too.
 */
function activeCompletedAt(dataset: DatasetDetail): string | null {
  if (dataset.active_version != null) {
    const match = dataset.versions.find(
      (v) => v.version === dataset.active_version,
    );
    if (match?.completed_at) return match.completed_at;
  }
  return dataset.last_refreshed_at ?? null;
}

/** Format an ISO timestamp as a readable local date-time, or `null`. */
function formatDateTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime();
  if (Number.isNaN(ms)) return null;
  return new Date(ms).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** A labelled `<time>` for an ISO date; renders a dash placeholder when absent. */
function DateValue({
  iso,
  testid,
}: {
  iso: string | null | undefined;
  testid: string;
}) {
  const text = formatDateTime(iso);
  if (text == null || !iso) {
    return (
      <span data-testid={testid} className="dataset-header__date dataset-header__date--empty">
        —
      </span>
    );
  }
  return (
    <time data-testid={testid} className="dataset-header__date" dateTime={iso}>
      {text}
    </time>
  );
}

/** The inline-rename editor + the "Rename" affordance (Requirement 1.1). */
function NameField({ dataset }: { dataset: DatasetDetail }) {
  const rename = useRenameDataset();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(dataset.name);
  const inputRef = useRef<HTMLInputElement>(null);

  // Keep the draft in sync when the dataset name changes under us (e.g. a live
  // patch) while not editing.
  useEffect(() => {
    if (!editing) setValue(dataset.name);
  }, [dataset.name, editing]);

  // Focus + select the input when editing opens.
  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  const open = () => {
    setValue(dataset.name);
    rename.reset();
    setEditing(true);
  };

  const cancel = () => {
    setEditing(false);
    setValue(dataset.name);
    rename.reset();
  };

  const save = () => {
    const next = value.trim();
    // Nothing to do on an empty or unchanged name: just close.
    if (next.length === 0 || next === dataset.name) {
      cancel();
      return;
    }
    rename.mutate(
      { id: dataset.id, name: next },
      { onSuccess: () => setEditing(false) },
    );
  };

  if (!editing) {
    return (
      <div className="dataset-header__name" data-testid="dataset-name">
        <h1 className="dataset-header__title" data-testid="dataset-name-text">
          {dataset.name}
        </h1>
        <button
          type="button"
          className="dataset-header__rename"
          data-testid="dataset-rename-button"
          onClick={open}
        >
          Rename
        </button>
      </div>
    );
  }

  return (
    <form
      className="dataset-header__name dataset-header__name--editing"
      data-testid="dataset-name-edit"
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
    >
      <label className="dataset-header__rename-label" htmlFor="dataset-rename-input">
        Dataset name
      </label>
      <input
        id="dataset-rename-input"
        ref={inputRef}
        className="dataset-header__rename-input"
        data-testid="dataset-rename-input"
        value={value}
        disabled={rename.isPending}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            cancel();
          }
        }}
      />
      <button
        type="submit"
        className="dataset-header__rename-save"
        data-testid="dataset-rename-save"
        disabled={rename.isPending || value.trim().length === 0}
      >
        Save
      </button>
      <button
        type="button"
        className="dataset-header__rename-cancel"
        data-testid="dataset-rename-cancel"
        disabled={rename.isPending}
        onClick={cancel}
      >
        Cancel
      </button>
      {rename.isError && (
        <span
          className="dataset-header__rename-error"
          data-testid="dataset-rename-error"
          role="alert"
        >
          {rename.error.message}
        </span>
      )}
    </form>
  );
}

/** The URL source block: main link, "Resolved to" line, and redirect hops. */
function UrlSource({ dataset }: { dataset: DatasetDetail }) {
  const [hopsOpen, setHopsOpen] = useState(false);
  const original = dataset.original_url ?? dataset.main_url ?? "";
  const final = dataset.final_url;
  const resolved = final != null && final.length > 0 && final !== original;
  const hops: Hop[] = dataset.status_detail?.redirects ?? [];

  return (
    <div className="dataset-header__source" data-testid="dataset-source-url">
      <a
        className="dataset-header__url"
        data-testid="dataset-original-url"
        href={original}
        target="_blank"
        rel="noreferrer noopener"
      >
        {original}
      </a>

      {resolved && (
        <div className="dataset-header__resolved" data-testid="dataset-resolved">
          <span className="dataset-header__resolved-label">Resolved to: </span>
          <a
            className="dataset-header__url dataset-header__url--final"
            data-testid="dataset-final-url"
            href={final as string}
            target="_blank"
            rel="noreferrer noopener"
          >
            {final}
          </a>
          {hops.length > 0 && (
            <button
              type="button"
              className="dataset-header__hops-toggle"
              data-testid="dataset-hops-toggle"
              aria-expanded={hopsOpen}
              aria-controls="dataset-hops"
              onClick={() => setHopsOpen((open) => !open)}
            >
              {hopsOpen ? "Hide redirect hops" : "Show redirect hops"}
            </button>
          )}
        </div>
      )}

      {resolved && hopsOpen && hops.length > 0 && (
        <ol className="dataset-header__hops" id="dataset-hops" data-testid="dataset-hops">
          {hops.map((hop, index) => (
            <li
              key={`${hop.url}-${index}`}
              className="dataset-header__hop"
              data-testid="dataset-hop"
            >
              <span className="dataset-header__hop-url">{hop.url}</span>
              {hop.status != null && (
                <span className="dataset-header__hop-status">{hop.status}</span>
              )}
              {hop.kind && (
                <span className="dataset-header__hop-kind">{hop.kind}</span>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** The upload source block: file name and optional description (Req 1.3). */
function UploadSource({ dataset }: { dataset: DatasetDetail }) {
  // The upload file name is stored as the dataset name (library `_main_url`),
  // so the row's `main_url` is the file name; fall back to the name.
  const fileName = dataset.main_url ?? dataset.name;
  const description = dataset.description;
  return (
    <div className="dataset-header__source" data-testid="dataset-source-upload">
      <span className="dataset-header__file-label">Uploaded file: </span>
      <span className="dataset-header__file-name" data-testid="dataset-file-name">
        {fileName}
      </span>
      {description != null && description.trim().length > 0 && (
        <p
          className="dataset-header__file-description"
          data-testid="dataset-file-description"
        >
          {description}
        </p>
      )}
    </div>
  );
}

export default function DatasetHeader({ dataset }: DatasetHeaderProps) {
  const completedAt = activeCompletedAt(dataset);
  const hasActiveVersion = dataset.active_version != null;

  return (
    <header className="dataset-header" data-testid="dataset-header">
      <div className="dataset-header__top">
        <NameField dataset={dataset} />
        <StatusBadge
          displayState={dataset.display_state}
          lastMessage={dataset.last_message}
          refreshChecking={dataset.refresh_check_id != null}
        />
      </div>

      {isUpload(dataset) ? (
        <UploadSource dataset={dataset} />
      ) : (
        <UrlSource dataset={dataset} />
      )}

      <dl className="dataset-header__meta" data-testid="dataset-meta">
        {dataset.platform && (
          <div className="dataset-header__meta-item">
            <dt className="dataset-header__meta-label">Platform</dt>
            <dd className="dataset-header__meta-value" data-testid="dataset-platform">
              {dataset.platform}
            </dd>
          </div>
        )}
        <div className="dataset-header__meta-item">
          <dt className="dataset-header__meta-label">Requested</dt>
          <dd className="dataset-header__meta-value">
            <DateValue iso={dataset.requested_at} testid="dataset-requested-at" />
          </dd>
        </div>
        <div className="dataset-header__meta-item">
          <dt className="dataset-header__meta-label">Last updated</dt>
          <dd className="dataset-header__meta-value">
            <DateValue iso={dataset.last_refreshed_at} testid="dataset-updated-at" />
          </dd>
        </div>
      </dl>

      {/* "Data as of {completed_at} · v{active_version}" for the shown version. */}
      {hasActiveVersion && (
        <p className="dataset-header__data-as-of" data-testid="dataset-data-as-of">
          <span className="dataset-header__data-as-of-label">Data as of </span>
          <DateValue iso={completedAt} testid="dataset-data-as-of-date" />
          <span className="dataset-header__data-as-of-sep"> · </span>
          <span className="dataset-header__data-as-of-version" data-testid="dataset-active-version">
            {`v${dataset.active_version}`}
          </span>
        </p>
      )}
    </header>
  );
}
