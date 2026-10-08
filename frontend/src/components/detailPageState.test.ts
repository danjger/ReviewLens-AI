/**
 * Unit tests for {@link deriveDetailPageState} (ingestion-summary task 6.3,
 * Requirements 6.2 and 6.4).
 *
 * These exercise the pure live-state derivation directly (no DOM): the three
 * live states and the "still showing v{n}" / chat-disabled decisions. The
 * DOM-level wiring is covered by the DatasetDetailPage component tests.
 */
import { describe, expect, it } from "vitest";

import { deriveDetailPageState } from "./detailPageState";

describe("deriveDetailPageState — first-version processing (Requirement 6.2)", () => {
  it("shows skeletons, the progress line, and disables chat when no version is active", () => {
    const state = deriveDetailPageState({
      status: "processing",
      activeVersion: null,
      displayState: "processing",
      lastMessage: "Fetching page 2 of 10",
    });

    expect(state.phase).toBe("first_version_processing");
    expect(state.showSkeletons).toBe(true);
    expect(state.chatDisabled).toBe(true);
    expect(state.showProgress).toBe(true);
    expect(state.showFailure).toBe(false);
    expect(state.stillShowingVersion).toBeNull();
    expect(state.message).toBe("Fetching page 2 of 10");
  });

  it("treats a `requested` status with no version the same as processing", () => {
    const state = deriveDetailPageState({
      status: "requested",
      activeVersion: null,
      displayState: "processing",
      lastMessage: null,
    });

    expect(state.phase).toBe("first_version_processing");
    expect(state.chatDisabled).toBe(true);
    // A null/blank message is normalised to null (the view supplies a default).
    expect(state.message).toBeNull();
  });
});

describe("deriveDetailPageState — active / refreshing (Requirement 6.2)", () => {
  it("is fully usable (data + chat) when a version is active", () => {
    const state = deriveDetailPageState({
      status: "updated",
      activeVersion: 3,
      displayState: "ready",
      lastMessage: "Processed 212 reviews",
    });

    expect(state.phase).toBe("active");
    expect(state.showSkeletons).toBe(false);
    expect(state.chatDisabled).toBe(false);
    expect(state.showProgress).toBe(false);
    expect(state.showFailure).toBe(false);
  });

  it("keeps data and chat available while a refresh runs on an active version", () => {
    const state = deriveDetailPageState({
      status: "processing",
      activeVersion: 2,
      displayState: "ready_refreshing",
      lastMessage: "Refreshing…",
    });

    // A refresh in progress still has an active version → fully usable; the
    // refreshing note is ReadinessBanner's job, not a skeleton here.
    expect(state.phase).toBe("active");
    expect(state.showSkeletons).toBe(false);
    expect(state.chatDisabled).toBe(false);
  });
});

describe("deriveDetailPageState — failed (Requirement 6.4)", () => {
  it("shows the failure + disables chat with no active version", () => {
    const state = deriveDetailPageState({
      status: "failed",
      activeVersion: null,
      displayState: "failed",
      lastMessage: "Page failed to load",
    });

    expect(state.phase).toBe("failed_no_version");
    expect(state.showFailure).toBe(true);
    expect(state.showSkeletons).toBe(true);
    expect(state.chatDisabled).toBe(true);
    expect(state.stillShowingVersion).toBeNull();
    expect(state.message).toBe("Page failed to load");
  });

  it("keeps the earlier version's data + chat and reports 'still showing v{n}' on a failed refresh", () => {
    const state = deriveDetailPageState({
      status: "failed",
      activeVersion: 2,
      displayState: "ready_refresh_failed",
      lastMessage: "Refresh failed: timeout",
    });

    expect(state.phase).toBe("failed_showing_previous");
    expect(state.showFailure).toBe(true);
    // The earlier version is still active → data + chat stay available.
    expect(state.showSkeletons).toBe(false);
    expect(state.chatDisabled).toBe(false);
    expect(state.stillShowingVersion).toBe(2);
  });
});
