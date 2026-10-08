/**
 * CitationChip — one citation rendered as a chip with an accessible review
 * popover (guardrailed-chat task 6.2, Requirement 2.2).
 *
 * An Exchange cites supporting reviews inline as `[r_0001]`; the UI renders each
 * citation (from the Exchange's `citations` list) as a chip. On hover, focus, or
 * tap the chip reveals the cited review's **text, rating, and date** — read from
 * the snippet that was **saved with the Exchange** (`citation_snippets[id]`), not
 * re-looked-up. Review IDs are only unique within one data version, so a stale
 * Exchange's popover keeps pointing at the right review after a refresh
 * (Requirement 2.2; design "Frontend (ChatPanel)" / "Post-processing").
 *
 * **The review text is page text, never generated** (product rule): the popover
 * renders `snippet.text` verbatim, the same way {@link SampleReviews} does.
 *
 * **Accessibility.** The chip is a real `<button>` so it is reachable and
 * operable by keyboard. The popover is shown on pointer hover *and* on keyboard
 * focus (so it is not hover-only), dismissed on blur, mouse-leave, or Escape,
 * and linked to the chip with `aria-describedby` + `aria-expanded`, so a screen
 * reader announces the review when the chip is focused. On a touch device a tap
 * focuses the chip, which shows the popover (tap-to-show).
 *
 * **Missing snippet (defensive).** A citation should always have a saved
 * snippet, but if one is absent (`snippet == null`) the chip still renders — in
 * a muted, non-interactive state with no popover and no `aria-describedby` —
 * rather than throwing. This should not happen in practice (post-processing
 * attaches a snippet for every surviving citation) but keeps the row robust.
 */
import { useCallback, useEffect, useId, useRef, useState } from "react";

import type { CitationSnippet } from "../api/chat";

export interface CitationChipProps {
  /** The cited review id, e.g. `r_0012` (shown on the chip). */
  id: string;
  /**
   * The snippet saved with the Exchange for this id, or null/undefined when
   * one is missing (defensive — renders a muted chip without a popover).
   */
  snippet?: CitationSnippet | null;
}

export default function CitationChip({ id, snippet }: CitationChipProps) {
  const [open, setOpen] = useState(false);
  const popoverId = useId();
  const rootRef = useRef<HTMLSpanElement>(null);

  const close = useCallback(() => setOpen(false), []);

  // Dismiss on Escape while open, matching the row-actions popover behaviour.
  useEffect(() => {
    if (!open) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") close();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, close]);

  // Missing snippet: a muted, non-interactive chip with no popover. Still shows
  // the id so the citation remains visible and traceable.
  if (snippet == null) {
    return (
      <span
        className="citation-chip citation-chip--muted"
        data-testid="citation-chip"
        data-citation-id={id}
        data-has-snippet="false"
      >
        [{id}]
      </span>
    );
  }

  return (
    <span
      className="citation-chip"
      data-testid="citation-chip"
      data-citation-id={id}
      data-has-snippet="true"
      ref={rootRef}
      // Pointer hover shows/hides; focus-within is handled by the button's
      // focus/blur so the popover is not hover-only (keyboard-accessible).
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        type="button"
        className="citation-chip__button"
        data-testid="citation-chip-button"
        aria-expanded={open}
        aria-describedby={open ? popoverId : undefined}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
      >
        [{id}]
      </button>
      {open && (
        <span
          className="citation-chip__popover"
          data-testid="citation-popover"
          data-citation-id={id}
          id={popoverId}
          role="tooltip"
        >
          {/* Review text is literal page text (never generated), rendered
              verbatim like SampleReviews. */}
          <span className="citation-chip__text" data-testid="citation-popover-text">
            {snippet.text}
          </span>
          {(snippet.rating != null || snippet.date != null) && (
            <span className="citation-chip__meta">
              {snippet.rating != null && (
                <span className="citation-chip__rating" data-testid="citation-popover-rating">
                  {snippet.rating}★
                </span>
              )}
              {snippet.date != null && (
                <span className="citation-chip__date" data-testid="citation-popover-date">
                  {snippet.date}
                </span>
              )}
            </span>
          )}
        </span>
      )}
    </span>
  );
}
