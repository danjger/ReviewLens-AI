/**
 * Unit tests for {@link deriveChatAvailability} / {@link chatAvailabilityFor}
 * (guardrailed-chat task 9).
 *
 * These pin the detail→availability mapping documented in {@link ChatInput}'s
 * module docstring and used by the {@link DatasetDetailPage} to drive the
 * mounted {@link ChatPanel}. The rule is pure, so it is exercised directly here
 * (no DOM); the E2E test then confirms the mounted panel reflects it.
 */
import { describe, expect, it } from "vitest";

import { makeDatasetDetail } from "../test/fixtures";
import { chatAvailabilityFor, deriveChatAvailability } from "./chatAvailability";

describe("deriveChatAvailability (Requirement 1)", () => {
  it("maps an archived dataset to `archived`, even with an active version", () => {
    // Archived wins over everything (Requirement 1.3: read-only history).
    expect(
      deriveChatAvailability({
        archived: true,
        activeVersion: 3,
        displayState: "ready",
      }),
    ).toBe("archived");
  });

  it("maps no active version to `no_active_version` (Requirement 1.2)", () => {
    expect(
      deriveChatAvailability({
        archived: false,
        activeVersion: null,
        displayState: "processing",
      }),
    ).toBe("no_active_version");
  });

  it("maps an active version while a refresh runs to `refreshing` (Requirement 1.1)", () => {
    expect(
      deriveChatAvailability({
        archived: false,
        activeVersion: 2,
        displayState: "ready_refreshing",
      }),
    ).toBe("refreshing");
  });

  it("maps an active version after a failed refresh to `refresh_failed` (Requirement 1.1)", () => {
    expect(
      deriveChatAvailability({
        archived: false,
        activeVersion: 2,
        displayState: "ready_refresh_failed",
      }),
    ).toBe("refresh_failed");
  });

  it("maps a healthy active dataset to `available`", () => {
    expect(
      deriveChatAvailability({
        archived: false,
        activeVersion: 3,
        displayState: "ready",
      }),
    ).toBe("available");
  });

  it("prefers `archived` over the no-active-version guard", () => {
    // An archived dataset with no active version is still read-only, not
    // "opening when processing finishes".
    expect(
      deriveChatAvailability({
        archived: true,
        activeVersion: null,
        displayState: "failed",
      }),
    ).toBe("archived");
  });
});

describe("chatAvailabilityFor (from a detail record)", () => {
  it("derives from `archived_at`, `active_version`, and `display_state`", () => {
    expect(chatAvailabilityFor(makeDatasetDetail({ active_version: 3, display_state: "ready" }))).toBe(
      "available",
    );
    expect(
      chatAvailabilityFor(
        makeDatasetDetail({ archived_at: "2026-09-02T00:00:00Z" }),
      ),
    ).toBe("archived");
    expect(
      chatAvailabilityFor(
        makeDatasetDetail({ active_version: null, display_state: "processing" }),
      ),
    ).toBe("no_active_version");
    expect(
      chatAvailabilityFor(
        makeDatasetDetail({ active_version: 2, display_state: "ready_refreshing" }),
      ),
    ).toBe("refreshing");
    expect(
      chatAvailabilityFor(
        makeDatasetDetail({
          active_version: 2,
          display_state: "ready_refresh_failed",
        }),
      ),
    ).toBe("refresh_failed");
  });
});
