/**
 * HtmlTab — the contents of the New Dataset panel's HTML tab
 * (dataset-ingestion task 21, Requirement 8.14).
 *
 * The analyst saves a review page's rendered HTML in their own browser and
 * uploads it here, for sites the app's own capture cannot read. The flow mirrors
 * the design's "HTML tab" bullet:
 *
 *   dropzone (`.html`/`.htm`/`.mhtml`) → pre-signed upload with a progress bar
 *   (reusing `POST /uploads`) → `POST /ingest/html-checks` to start the
 *   assessment → a verdict card IDENTICAL to the URL card (verdict badge with
 *   text not color alone, reasons, warnings, evidence, two or three verified
 *   sample reviews copied from the uploaded page) → a required name field plus
 *   optional source URL and description fields → Add.
 *
 * It composes the existing pieces unchanged:
 * - {@link UploadDropzone} (configured for saved-page extensions) and
 *   {@link UploadProgressBar} for the pre-signed upload (Requirement 8.1);
 * - {@link useHtmlUpload} for the upload → start-check producer steps;
 * - {@link useCheck} to poll the one-item Check Session and
 *   {@link useRealtime} so that single item listens on the same `check.updated`
 *   channel as a URL check (design "HTML tab");
 * - {@link VerdictCard} for the verdict (same card as the URL tab, so the
 *   badge, reasons, warnings, evidence, samples, and the "Already tracked …"
 *   note when a supplied source URL is tracked are all identical);
 * - {@link useAddItems} + {@link AddConfirmDialog} for Add, carrying the
 *   required `name` and optional `source_url`/`description` through the Add body
 *   (threaded through the backend in task 20).
 *
 * Add is disabled until a non-empty name is entered and the item has an addable
 * verdict (`will_work`/`limited`); `wont_work` is never addable. A `limited`
 * verdict routes through the confirmation first (Requirement 3.9).
 */
import { useCallback, useMemo, useState } from "react";

import type { CheckItem } from "../api/ingest";
import { useAddItems, type UseAddItemsOptions } from "../hooks/useAddItems";
import { useCheck } from "../hooks/useCheck";
import { useCheckParam } from "../hooks/useCheckParam";
import { useHtmlUpload, type UseHtmlUploadOptions } from "../hooks/useHtmlUpload";
import { useRealtime } from "../hooks/useRealtime";
import AddConfirmDialog from "./AddConfirmDialog";
import AddResultsSummary from "./AddResultsSummary";
import RateLimitMessage from "./RateLimitMessage";
import UploadDropzone from "./UploadDropzone";
import UploadProgressBar from "./UploadProgressBar";
import VerdictCard from "./VerdictCard";

/** Accepted saved-page extensions (Requirement 8.1, 8.14). */
const HTML_EXTENSIONS = [".html", ".htm", ".mhtml"] as const;

export interface HtmlTabProps {
  /** Navigation override for the Add flow (tests inject this). */
  addNavigation?: UseAddItemsOptions;
  /** Overrides for the upload producer (tests inject the uploader). */
  htmlUpload?: UseHtmlUploadOptions;
  /** Disable the real-time channel (tests that don't drive a socket). */
  realtimeEnabled?: boolean;
}

/** True when the item's verdict is one that can actually be added. */
function isAddable(item: CheckItem | undefined): boolean {
  const label = item?.verdict?.verdict;
  return label === "will_work" || label === "limited";
}

export default function HtmlTab({
  addNavigation,
  htmlUpload,
  realtimeEnabled = true,
}: HtmlTabProps) {
  const { checkId, setCheckId } = useCheckParam();

  // The single assessment item polls / listens on the same `check.updated`
  // channel as a URL check (design "HTML tab").
  useRealtime({ enabled: realtimeEnabled });

  const { items } = useCheck(checkId, setCheckId);
  const item = items[0];

  const [sourceUrl, setSourceUrl] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [confirming, setConfirming] = useState(false);

  const upload = useHtmlUpload({
    ...htmlUpload,
    onCheckCreated: (newCheckId) => {
      setCheckId(newCheckId);
      htmlUpload?.onCheckCreated?.(newCheckId, "");
    },
  });

  // Note: the HTML producer creates the Check Session server-side (via
  // `POST /ingest/html-checks`), so this tab only puts the id in `?check=` and
  // polls it through `useCheck`; `useCheck`'s create path (the URL batch) is
  // deliberately unused here.
  const {
    add,
    results,
    isAdding,
    rateLimit: addRateLimit,
    error: addError,
  } = useAddItems(checkId, addNavigation);

  const handleFile = useCallback(
    (file: File) => {
      // Starting a new upload clears any previous assessment + form state.
      setCheckId(null);
      setName("");
      setDescription("");
      setConfirming(false);
      upload.start({ file, sourceUrl: sourceUrl.trim() || undefined });
    },
    [upload, sourceUrl, setCheckId],
  );

  const showProgress =
    upload.phase === "requesting" || upload.phase === "uploading";

  const trimmedName = name.trim();
  const verdictLabel = item?.verdict?.verdict;
  const canAdd =
    checkId != null &&
    item != null &&
    isAddable(item) &&
    trimmedName.length > 0 &&
    !isAdding;

  const submit = useCallback(
    (confirmLimited: boolean) => {
      if (item == null) return;
      add([
        {
          item_id: item.item_id,
          confirm_limited: confirmLimited,
          name: trimmedName,
          source_url: sourceUrl.trim() || undefined,
          description: description.trim() || undefined,
        },
      ]);
    },
    [add, item, trimmedName, sourceUrl, description],
  );

  const handleAddClick = useCallback(() => {
    if (!canAdd) return;
    if (verdictLabel === "limited") {
      setConfirming(true);
      return;
    }
    submit(false);
  }, [canAdd, verdictLabel, submit]);

  const handleConfirm = useCallback(() => {
    setConfirming(false);
    submit(true);
  }, [submit]);

  const handleCancel = useCallback(() => setConfirming(false), []);

  // A no-op toggle/retry: the HTML tab's single verdict card is not selectable
  // (Add gating lives below), but VerdictCard requires the handlers.
  const noop = useCallback(() => {}, []);

  // VerdictCard shows `item.input` as the "URL"; for an HTML upload the backend
  // sets `input` to an internal `upload:{id}` reference, so display the uploaded
  // file name in its place (Requirement 8.10).
  const displayItem = useMemo<CheckItem | undefined>(
    () =>
      item ? { ...item, input: upload.fileName ?? item.input } : undefined,
    [item, upload.fileName],
  );

  // The item for the confirmation dialog carries the analyst's name as its
  // display label (the dialog shows `item.input`, the internal upload ref for
  // an HTML upload), so the confirmation reads naturally.
  const confirmItems = useMemo(
    () =>
      item
        ? [{ ...item, input: trimmedName || upload.fileName || item.input }]
        : [],
    [item, trimmedName, upload.fileName],
  );

  return (
    <section className="html-tab" data-testid="html-tab">
      {/* The optional source URL is captured BEFORE the upload so it rides on
          `POST /ingest/html-checks` and drives the duplicate lookup / "Already
          tracked" note (Requirements 8.12, 6.3; design sequence diagram). It is
          disabled once an upload is in flight so it can't change mid-check. */}
      <div className="html-tab__source-url">
        <label htmlFor="html-source-url">Source URL (optional)</label>
        <input
          id="html-source-url"
          type="url"
          data-testid="html-source-url-input"
          value={sourceUrl}
          disabled={upload.isBusy || item != null}
          onChange={(event) => setSourceUrl(event.target.value)}
        />
      </div>

      <UploadDropzone
        onFile={handleFile}
        fileName={upload.fileName}
        disabled={upload.isBusy}
        extensions={HTML_EXTENSIONS}
        prompt="Drop a saved page (.html, .htm, or .mhtml) here, or click to choose one"
        rejectMessage="Only .html, .htm, and .mhtml saved pages can be uploaded."
      />

      {showProgress && (
        <UploadProgressBar
          value={upload.phase === "requesting" ? 0 : upload.progress}
        />
      )}

      {upload.rateLimit && <RateLimitMessage error={upload.rateLimit} />}

      {upload.errorMessage && (
        <p className="html-tab__error" data-testid="html-tab-error" role="alert">
          {upload.errorMessage}
        </p>
      )}

      {item && (
        <ul className="html-tab__results" data-testid="html-tab-results">
          <VerdictCard
            item={displayItem ?? item}
            included={isAddable(item)}
            onToggleInclude={noop}
            onRetry={noop}
          />
        </ul>
      )}

      {item && (
        <div className="html-tab__form" data-testid="html-tab-form">
          <div className="html-tab__name">
            <label htmlFor="html-name">
              Dataset name <span aria-hidden="true">*</span>
            </label>
            <input
              id="html-name"
              type="text"
              data-testid="html-name-input"
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
            />
          </div>

          <div className="html-tab__description">
            <label htmlFor="html-description">Source description (optional)</label>
            <textarea
              id="html-description"
              data-testid="html-description-input"
              rows={2}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>

          <button
            type="button"
            data-testid="html-add-button"
            disabled={!canAdd}
            onClick={handleAddClick}
          >
            {isAdding ? "Adding…" : "Add"}
          </button>

          {addRateLimit && <RateLimitMessage error={addRateLimit} />}

          {addError && (
            <p className="html-tab__add-error" data-testid="html-add-error" role="alert">
              {addError.message}
            </p>
          )}
        </div>
      )}

      {confirming && (
        <AddConfirmDialog
          items={confirmItems}
          onConfirm={handleConfirm}
          onCancel={handleCancel}
        />
      )}

      {results && <AddResultsSummary results={results} items={items} />}
    </section>
  );
}
