/**
 * SampleReviews — the two or three expandable sample reviews on a verdict card
 * (Requirement 3.7).
 *
 * The samples are literal page text read by the backend (never AI-generated),
 * so they are rendered verbatim. The list is collapsed behind a toggle so the
 * card stays compact; expanding reveals each sample's text and, when present,
 * its rating and date.
 */
import { useState } from "react";

import type { Sample } from "../api/ingest";

export interface SampleReviewsProps {
  samples: Sample[];
}

export default function SampleReviews({ samples }: SampleReviewsProps) {
  const [expanded, setExpanded] = useState(false);

  if (samples.length === 0) {
    return null;
  }

  return (
    <div className="sample-reviews" data-testid="sample-reviews">
      <button
        type="button"
        data-testid="toggle-samples"
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
      >
        {expanded ? "Hide sample reviews" : "Show sample reviews"}
      </button>
      {expanded && (
        <ul data-testid="sample-list">
          {samples.map((sample, index) => (
            <li key={index} data-testid="sample-review">
              <p data-testid="sample-text">{sample.text}</p>
              {(sample.rating != null || sample.date != null) && (
                <p className="sample-meta">
                  {sample.rating != null && (
                    <span data-testid="sample-rating">{sample.rating}★</span>
                  )}
                  {sample.date != null && (
                    <span data-testid="sample-date">{sample.date}</span>
                  )}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
