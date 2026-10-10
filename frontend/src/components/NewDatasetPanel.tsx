/**
 * NewDatasetPanel — the "Add new reviews" half of the Library (dataset-library
 * task 6.2, Requirements 1.2, 1.5, 1.6).
 *
 * This is the SHELL only: the tab contents are the existing dataset-ingestion
 * components, hosted unchanged — {@link UrlTab} and {@link UploadTab}. The panel
 * owns:
 * - the URL / Upload tab switch;
 * - the collapse/expand behavior (Requirement 1.5): once the library has at
 *   least one dataset the panel can collapse to a single "+ Add new reviews"
 *   bar; it starts expanded when the library is empty (Requirement 1.6) and
 *   expands whenever a Check is in progress (a `?check=` id is present);
 * - a tinted visual treatment (a distinct `className` + the "+" heading) so it
 *   can never be confused with the plain Tracked table (Requirement 1.1).
 *
 * It is the ONLY place new URLs/files are entered (Requirement 1.2); the Tracked
 * section has no inputs (Requirement 1.3).
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import type { UseAddItemsOptions } from "../hooks/useAddItems";
import { CHECK_PARAM } from "../hooks/useCheckParam";
import HtmlTab from "./HtmlTab";
import UploadTab from "./UploadTab";
import UrlTab from "./UrlTab";

type TabKey = "url" | "upload" | "html";

export interface NewDatasetPanelProps {
  /**
   * Start expanded (true when the library is empty — Requirement 1.6). Also
   * forces the panel open while there are no datasets to collapse behind.
   */
  defaultExpanded: boolean;
  /** True when there is at least one dataset (so the panel may collapse). */
  collapsible: boolean;
  /** Navigation override for the Add flow (the page records highlights here). */
  addNavigation?: UseAddItemsOptions;
}

/** Is a Check currently active (a `?check=` id in the URL)? */
function hasActiveCheck(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get(CHECK_PARAM) != null;
}

export default function NewDatasetPanel({
  defaultExpanded,
  collapsible,
  addNavigation,
}: NewDatasetPanelProps) {
  const [tab, setTab] = useState<TabKey>("url");
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [checkActive, setCheckActive] = useState(hasActiveCheck);

  // Track `?check=` so the panel auto-expands while a Check is in progress and
  // stays in sync with back/forward navigation (Requirement 1.5).
  useEffect(() => {
    function sync() {
      setCheckActive(hasActiveCheck());
    }
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);

  // When the library becomes empty, force the panel open (Requirement 1.6).
  useEffect(() => {
    if (!collapsible) setExpanded(true);
  }, [collapsible]);

  // The panel is open when the analyst expanded it, when it can't collapse
  // (empty library), or when a Check is running.
  const isOpen = expanded || !collapsible || checkActive;

  const handleToggle = useCallback(() => {
    setExpanded((value) => !value);
  }, []);

  // The URL / Upload tab switch, memoised on the active tab.
  const headingTab = useMemo(
    () => (
      <div className="new-dataset-panel__tabs" role="tablist" aria-label="Add method">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "url"}
          data-testid="tab-url"
          className={tab === "url" ? "is-active" : ""}
          onClick={() => setTab("url")}
        >
          URLs
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "upload"}
          data-testid="tab-upload"
          className={tab === "upload" ? "is-active" : ""}
          onClick={() => setTab("upload")}
        >
          Upload file
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "html"}
          data-testid="tab-html"
          className={tab === "html" ? "is-active" : ""}
          onClick={() => setTab("html")}
        >
          Saved page
        </button>
      </div>
    ),
    [tab],
  );

  return (
    <section
      id="add-new-reviews"
      className="new-dataset-panel"
      data-testid="new-dataset-panel"
      data-expanded={isOpen ? "true" : "false"}
      aria-labelledby="add-heading"
    >
      <header className="new-dataset-panel__header">
        <h2 id="add-heading" className="new-dataset-panel__title">
          <span aria-hidden="true" className="new-dataset-panel__plus">
            +
          </span>{" "}
          Add new reviews
        </h2>
        {collapsible && !checkActive && (
          <button
            type="button"
            className="new-dataset-panel__toggle"
            data-testid="panel-toggle"
            aria-expanded={isOpen}
            aria-controls="add-panel-body"
            onClick={handleToggle}
          >
            {isOpen ? "Collapse" : "Add new reviews"}
          </button>
        )}
      </header>

      {isOpen && (
        <div id="add-panel-body" className="new-dataset-panel__body">
          {headingTab}
          <div className="new-dataset-panel__tabpanel" role="tabpanel">
            {tab === "url" && (
              <UrlTab
                addNavigation={addNavigation}
                onSelectionChange={() => setCheckActive(hasActiveCheck())}
              />
            )}
            {tab === "upload" && <UploadTab />}
            {tab === "html" && <HtmlTab addNavigation={addNavigation} />}
          </div>
        </div>
      )}
    </section>
  );
}
