/**
 * Tests for {@link ProcessingTimeline} (ingestion-summary task 6.1,
 * Requirement 6.1).
 *
 * The timeline renders the `status_detail.events` log as a vertical list,
 * newest first, with each timestamp in a `<time dateTime>` element. Tests
 * target `data-testid`s and assert timestamps via the `dateTime` attribute (the
 * original ISO string) rather than the locale-formatted visible text
 * (testing.md: never depend on dates).
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeStatusEvent } from "../test/fixtures";
import ProcessingTimeline from "./ProcessingTimeline";

describe("ProcessingTimeline", () => {
  it("renders the timeline container and summary", () => {
    render(
      <ProcessingTimeline
        statusDetail={{ events: [makeStatusEvent()] }}
      />,
    );

    expect(screen.getByTestId("processing-timeline")).toBeInTheDocument();
    expect(screen.getByTestId("processing-timeline-summary")).toBeInTheDocument();
    expect(screen.getByTestId("processing-timeline-list")).toBeInTheDocument();
  });

  // ── Requirement 6.1: events with timestamps and messages ──────────────────

  it("lists each event's message", () => {
    render(
      <ProcessingTimeline
        statusDetail={{
          events: [
            makeStatusEvent({ message: "Requested" }),
            makeStatusEvent({ message: "Fetching page 1 of 10" }),
            makeStatusEvent({ message: "Processed 212 reviews" }),
          ],
        }}
      />,
    );

    // Newest first: last written event appears at index 0.
    expect(screen.getByTestId("timeline-event-0-message")).toHaveTextContent(
      "Processed 212 reviews",
    );
    expect(screen.getByTestId("timeline-event-1-message")).toHaveTextContent(
      "Fetching page 1 of 10",
    );
    expect(screen.getByTestId("timeline-event-2-message")).toHaveTextContent("Requested");
  });

  it("renders events newest-first regardless of the source order (oldest-first)", () => {
    render(
      <ProcessingTimeline
        statusDetail={{
          events: [
            makeStatusEvent({ message: "first", at: "2026-09-02T20:20:00Z" }),
            makeStatusEvent({ message: "second", at: "2026-09-02T20:21:00Z" }),
          ],
        }}
      />,
    );

    const items = screen.getAllByTestId(/^timeline-event-\d+$/);
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByTestId("timeline-event-0-message")).toHaveTextContent("second");
    expect(within(items[1]).getByTestId("timeline-event-1-message")).toHaveTextContent("first");
  });

  it("renders each timestamp as a <time dateTime> with the original ISO instant", () => {
    render(
      <ProcessingTimeline
        statusDetail={{
          events: [makeStatusEvent({ at: "2026-09-02T20:20:00Z", message: "Requested" })],
        }}
      />,
    );

    const time = screen.getByTestId("timeline-event-0-time");
    expect(time.tagName).toBe("TIME");
    // Assert on the machine-readable dateTime, not the locale-formatted text.
    expect(time).toHaveAttribute("dateTime", "2026-09-02T20:20:00Z");
  });

  it("renders the status chip for a status event", () => {
    render(
      <ProcessingTimeline
        statusDetail={{ events: [makeStatusEvent({ status: "processing" })] }}
      />,
    );

    expect(screen.getByTestId("timeline-event-0-status")).toHaveTextContent("processing");
    expect(screen.getByTestId("timeline-event-0")).toHaveAttribute("data-status", "processing");
  });

  it("omits the status chip for a progress-only event (null status)", () => {
    render(
      <ProcessingTimeline
        statusDetail={{ events: [makeStatusEvent({ status: null, message: "Fetching page 3" })] }}
      />,
    );

    expect(screen.queryByTestId("timeline-event-0-status")).not.toBeInTheDocument();
    expect(screen.getByTestId("timeline-event-0-message")).toHaveTextContent("Fetching page 3");
  });

  it("still renders an event with a missing/unparseable timestamp, without a <time>", () => {
    render(
      <ProcessingTimeline
        statusDetail={{ events: [makeStatusEvent({ at: null, message: "No timestamp" })] }}
      />,
    );

    expect(screen.getByTestId("timeline-event-0-message")).toHaveTextContent("No timestamp");
    expect(screen.queryByTestId("timeline-event-0-time")).not.toBeInTheDocument();
  });

  it("skips events with no usable message", () => {
    render(
      <ProcessingTimeline
        statusDetail={{
          events: [
            makeStatusEvent({ message: "" }),
            makeStatusEvent({ message: "   " }),
            makeStatusEvent({ message: "Real message" }),
          ],
        }}
      />,
    );

    // Only the one usable event survives, at index 0.
    expect(screen.getByTestId("timeline-event-0-message")).toHaveTextContent("Real message");
    expect(screen.queryByTestId("timeline-event-1")).not.toBeInTheDocument();
  });

  // ── Empty / missing handling ──────────────────────────────────────────────

  describe("empty and missing states", () => {
    it("shows the empty state for an empty events list", () => {
      render(<ProcessingTimeline statusDetail={{ events: [] }} />);

      expect(screen.getByTestId("processing-timeline-empty")).toBeInTheDocument();
      expect(screen.queryByTestId("processing-timeline-list")).not.toBeInTheDocument();
    });

    it("shows the empty state when status_detail is undefined", () => {
      render(<ProcessingTimeline />);

      expect(screen.getByTestId("processing-timeline")).toBeInTheDocument();
      expect(screen.getByTestId("processing-timeline-empty")).toBeInTheDocument();
    });

    it("shows the empty state when events is missing from status_detail", () => {
      render(<ProcessingTimeline statusDetail={{ redirects: [] }} />);

      expect(screen.getByTestId("processing-timeline-empty")).toBeInTheDocument();
    });

    it("shows the empty state when every event lacks a usable message", () => {
      render(
        <ProcessingTimeline
          statusDetail={{ events: [makeStatusEvent({ message: "" })] }}
        />,
      );

      expect(screen.getByTestId("processing-timeline-empty")).toBeInTheDocument();
      expect(screen.queryByTestId("processing-timeline-list")).not.toBeInTheDocument();
    });
  });
});
