/**
 * Tests for {@link RateLimitMessage} (dataset-ingestion task 9.4,
 * Requirement 1.6).
 *
 * When a Check is rate-limited the panel must say *when* checks can resume. The
 * component turns the `Retry-After` seconds carried on {@link ApiError} into a
 * plain-language resume time: a seconds phrasing under a minute, a rounded
 * minutes phrasing above, and a generic fallback when the delay is unknown or
 * non-positive.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/ingest";
import RateLimitMessage from "./RateLimitMessage";

/** Build a 429 ApiError carrying `retryAfterSeconds`. */
function rateLimited(seconds: number | null): ApiError {
  return new ApiError(429, "RATE_LIMITED", "Too many requests.", seconds);
}

describe("RateLimitMessage", () => {
  it("phrases a sub-minute delay in seconds", () => {
    render(<RateLimitMessage error={rateLimited(45)} />);
    expect(screen.getByTestId("rate-limit-message")).toHaveTextContent("45 seconds");
  });

  it("rounds a longer delay up to whole minutes", () => {
    render(<RateLimitMessage error={rateLimited(90)} />);
    // 90s → "about 2 minutes" (ceil).
    expect(screen.getByTestId("rate-limit-message")).toHaveTextContent("2 minutes");
  });

  it("uses the singular 'minute' at exactly one minute", () => {
    render(<RateLimitMessage error={rateLimited(60)} />);
    const text = screen.getByTestId("rate-limit-message").textContent ?? "";
    expect(text).toMatch(/1 minute\b/);
    expect(text).not.toMatch(/1 minutes/);
  });

  it("falls back to a generic message when the delay is unknown", () => {
    render(<RateLimitMessage error={rateLimited(null)} />);
    const msg = screen.getByTestId("rate-limit-message");
    expect(msg).toBeInTheDocument();
    expect(msg).toHaveTextContent(/wait a moment/i);
  });

  it("falls back to the generic message for a non-positive delay", () => {
    render(<RateLimitMessage error={rateLimited(0)} />);
    expect(screen.getByTestId("rate-limit-message")).toHaveTextContent(/wait a moment/i);
  });
});
