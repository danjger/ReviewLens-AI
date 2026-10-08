/**
 * Component tests for {@link DatasetHeader} (ingestion-summary task 2.2,
 * Requirements 1.1–1.4).
 *
 * Covers: a plain URL dataset, a resolved/redirect dataset with expandable
 * hops, an upload dataset (file name + optional description), each
 * `display_state` badge, and the inline-rename interaction (PATCH via MSW).
 *
 * Per testing.md, assertions target data-testid structure and never match on
 * text that includes counts or dates — dates are asserted via the `dateTime`
 * attribute / testid presence, and the version string is a stable `v{n}`.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { DatasetDetail, DisplayState } from "../api/datasets";
import type { Hop } from "../api/ingest";
import { makeDatasetDetail } from "../test/fixtures";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import DatasetHeader from "./DatasetHeader";

function renderHeader(dataset: DatasetDetail) {
  return renderWithClient(<DatasetHeader dataset={dataset} />);
}

describe("DatasetHeader — URL source (Req 1.2)", () => {
  it("shows the original URL as the main link and no resolved line when the final URL matches", () => {
    renderHeader(
      makeDatasetDetail({
        source_type: "url",
        original_url: "https://g2.com/acme/reviews",
        final_url: "https://g2.com/acme/reviews",
      }),
    );

    const original = screen.getByTestId("dataset-original-url");
    expect(original).toHaveAttribute("href", "https://g2.com/acme/reviews");
    expect(screen.queryByTestId("dataset-resolved")).not.toBeInTheDocument();
    expect(screen.queryByTestId("dataset-source-upload")).not.toBeInTheDocument();
  });

  it("shows a 'Resolved to' line and expandable redirect hops when the final URL differs (Req 1.2)", async () => {
    const user = userEvent.setup();
    const redirects: Hop[] = [
      { url: "https://g2.com/acme", status: 301 },
      { url: "https://www.g2.com/acme/reviews", status: 302, kind: "client" },
    ];
    renderHeader(
      makeDatasetDetail({
        source_type: "url",
        original_url: "https://g2.com/acme",
        final_url: "https://www.g2.com/acme/reviews",
        status_detail: { events: [], redirects },
      }),
    );

    expect(screen.getByTestId("dataset-resolved")).toBeInTheDocument();
    expect(screen.getByTestId("dataset-final-url")).toHaveAttribute(
      "href",
      "https://www.g2.com/acme/reviews",
    );

    // Hops are collapsed until the toggle is clicked.
    expect(screen.queryByTestId("dataset-hops")).not.toBeInTheDocument();
    const toggle = screen.getByTestId("dataset-hops-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const list = screen.getByTestId("dataset-hops");
    expect(within(list).getAllByTestId("dataset-hop")).toHaveLength(2);
  });

  it("does not offer a hops toggle when there are no recorded redirects", () => {
    renderHeader(
      makeDatasetDetail({
        source_type: "url",
        original_url: "https://g2.com/acme",
        final_url: "https://www.g2.com/acme/reviews",
        status_detail: { events: [], redirects: [] },
      }),
    );

    expect(screen.getByTestId("dataset-resolved")).toBeInTheDocument();
    expect(screen.queryByTestId("dataset-hops-toggle")).not.toBeInTheDocument();
  });
});

describe("DatasetHeader — upload source (Req 1.3)", () => {
  it("shows the file name and description in place of URLs", () => {
    renderHeader(
      makeDatasetDetail({
        source_type: "upload",
        name: "acme-reviews.csv",
        main_url: "acme-reviews.csv",
        original_url: null,
        final_url: null,
        description: "Exported from the CRM on launch day.",
      }),
    );

    expect(screen.getByTestId("dataset-source-upload")).toBeInTheDocument();
    expect(screen.getByTestId("dataset-file-name")).toHaveTextContent(
      "acme-reviews.csv",
    );
    expect(screen.getByTestId("dataset-file-description")).toHaveTextContent(
      "Exported from the CRM on launch day.",
    );
    // No URL links for an upload.
    expect(screen.queryByTestId("dataset-original-url")).not.toBeInTheDocument();
    expect(screen.queryByTestId("dataset-source-url")).not.toBeInTheDocument();
  });

  it("omits the description block when there is no description", () => {
    renderHeader(
      makeDatasetDetail({
        source_type: "upload",
        name: "acme-reviews.csv",
        main_url: "acme-reviews.csv",
        original_url: null,
        final_url: null,
        description: null,
      }),
    );

    expect(screen.getByTestId("dataset-file-name")).toBeInTheDocument();
    expect(
      screen.queryByTestId("dataset-file-description"),
    ).not.toBeInTheDocument();
  });
});

describe("DatasetHeader — meta and version (Req 1.4)", () => {
  it("shows the platform, request date, last-updated date, and the data-as-of version line", () => {
    renderHeader(
      makeDatasetDetail({
        platform: "g2",
        active_version: 3,
        requested_at: "2026-09-01T00:00:00Z",
        last_refreshed_at: "2026-09-02T20:20:00Z",
        versions: [
          {
            version: 3,
            status: "updated",
            review_count: 212,
            completed_at: "2026-09-02T20:20:00Z",
          },
        ],
      }),
    );

    expect(screen.getByTestId("dataset-platform")).toHaveTextContent("g2");
    // Dates asserted via presence + dateTime attribute, not formatted text.
    expect(screen.getByTestId("dataset-requested-at")).toHaveAttribute(
      "datetime",
      "2026-09-01T00:00:00Z",
    );
    expect(screen.getByTestId("dataset-updated-at")).toHaveAttribute(
      "datetime",
      "2026-09-02T20:20:00Z",
    );
    expect(screen.getByTestId("dataset-data-as-of")).toBeInTheDocument();
    expect(screen.getByTestId("dataset-active-version")).toHaveTextContent("v3");
  });

  it("hides the data-as-of line when there is no active version", () => {
    renderHeader(
      makeDatasetDetail({ active_version: null, versions: [] }),
    );

    expect(screen.queryByTestId("dataset-data-as-of")).not.toBeInTheDocument();
  });
});

describe("DatasetHeader — status badge (Req 1.4)", () => {
  const states: DisplayState[] = [
    "processing",
    "ready",
    "ready_refreshing",
    "ready_refresh_failed",
    "failed",
  ];

  it.each(states)("renders the %s display-state badge", (state) => {
    renderHeader(
      makeDatasetDetail({
        display_state: state,
        active_version: state === "processing" || state === "failed" ? null : 3,
        refresh_check_id: null,
      }),
    );

    expect(screen.getByTestId("status-badge")).toHaveAttribute(
      "data-state",
      state,
    );
  });
});

describe("DatasetHeader — inline rename (Req 1.1)", () => {
  it("opens the editor and PATCHes the new name on save", async () => {
    const user = userEvent.setup();
    let patchedBody: { name?: string } | null = null;
    server.use(
      http.patch("/api/datasets/ds-1", async ({ request }) => {
        patchedBody = (await request.json()) as { name?: string };
        return HttpResponse.json(
          { ...makeDatasetDetail({ id: "ds-1", name: "Renamed CRM" }) },
          { status: 200 },
        );
      }),
    );

    renderHeader(makeDatasetDetail({ id: "ds-1", name: "Acme CRM" }));

    await user.click(screen.getByTestId("dataset-rename-button"));
    const input = screen.getByTestId("dataset-rename-input");
    await user.clear(input);
    await user.type(input, "Renamed CRM");
    await user.click(screen.getByTestId("dataset-rename-save"));

    await waitFor(() => expect(patchedBody).toEqual({ name: "Renamed CRM" }));
  });

  it("cancels editing without a request and keeps the original name", async () => {
    const user = userEvent.setup();
    renderHeader(makeDatasetDetail({ id: "ds-1", name: "Acme CRM" }));

    await user.click(screen.getByTestId("dataset-rename-button"));
    const input = screen.getByTestId("dataset-rename-input");
    await user.clear(input);
    await user.type(input, "Discard me");
    await user.click(screen.getByTestId("dataset-rename-cancel"));

    expect(screen.getByTestId("dataset-name-text")).toHaveTextContent("Acme CRM");
    expect(screen.queryByTestId("dataset-rename-input")).not.toBeInTheDocument();
  });

  it("surfaces a rename error from the API", async () => {
    const user = userEvent.setup();
    server.use(
      http.patch("/api/datasets/ds-1", () =>
        HttpResponse.json(
          { error: { code: "VALIDATION", message: "A dataset name is required" } },
          { status: 422 },
        ),
      ),
    );

    renderHeader(makeDatasetDetail({ id: "ds-1", name: "Acme CRM" }));

    await user.click(screen.getByTestId("dataset-rename-button"));
    const input = screen.getByTestId("dataset-rename-input");
    await user.clear(input);
    await user.type(input, "New name");
    await user.click(screen.getByTestId("dataset-rename-save"));

    await waitFor(() =>
      expect(screen.getByTestId("dataset-rename-error")).toHaveTextContent(
        "A dataset name is required",
      ),
    );
  });
});
