/**
 * Unit + axe tests for {@link DatasetThumbnail} (dataset-library task 10.2).
 *
 * Covers the three source-aware renders: the snapshot image for a URL dataset
 * with a thumbnail URL, the CSV icon for an upload dataset, and the neutral
 * placeholder when there is no URL or the image fails to load.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { axe } from "jest-axe";
import { describe, expect, it } from "vitest";

import DatasetThumbnail from "./DatasetThumbnail";

describe("DatasetThumbnail", () => {
  it("renders the snapshot image for a URL dataset with a thumbnail URL", () => {
    render(
      <DatasetThumbnail
        sourceType="url"
        thumbnailUrl="https://signed.example/datasets/ds-1/snapshot/v2.png"
        name="Acme CRM"
      />,
    );
    const thumb = screen.getByTestId("dataset-thumbnail");
    expect(thumb).toHaveAttribute("data-kind", "image");
    const img = screen.getByTestId("dataset-thumbnail-img");
    expect(img).toHaveAttribute(
      "src",
      "https://signed.example/datasets/ds-1/snapshot/v2.png",
    );
    // Accessible, descriptive alt (not decorative-only).
    expect(img).toHaveAttribute("alt", "Rendered page for Acme CRM");
    // Lazy-loaded so a long list doesn't fetch every image up front.
    expect(img).toHaveAttribute("loading", "lazy");
  });

  it("renders a CSV icon for an upload dataset (never a page image)", () => {
    render(
      <DatasetThumbnail
        sourceType="upload"
        thumbnailUrl={null}
        name="support-tickets.csv"
      />,
    );
    const thumb = screen.getByTestId("dataset-thumbnail");
    expect(thumb).toHaveAttribute("data-kind", "csv");
    expect(thumb).toHaveAttribute("aria-label", "CSV upload: support-tickets.csv");
    expect(screen.queryByTestId("dataset-thumbnail-img")).toBeNull();
  });

  it("renders the placeholder for a URL dataset with no thumbnail yet", () => {
    render(
      <DatasetThumbnail sourceType="url" thumbnailUrl={null} name="New Co" />,
    );
    const thumb = screen.getByTestId("dataset-thumbnail");
    expect(thumb).toHaveAttribute("data-kind", "placeholder");
    expect(thumb).toHaveAttribute("aria-label", "No preview for New Co");
  });

  it("falls back to the placeholder (not a broken image) when the image errors", () => {
    render(
      <DatasetThumbnail
        sourceType="url"
        thumbnailUrl="https://signed.example/expired.png"
        name="Acme CRM"
      />,
    );
    const img = screen.getByTestId("dataset-thumbnail-img");
    fireEvent.error(img);
    const thumb = screen.getByTestId("dataset-thumbnail");
    expect(thumb).toHaveAttribute("data-kind", "placeholder");
    expect(screen.queryByTestId("dataset-thumbnail-img")).toBeNull();
  });

  it("prefers source_type over the URL: an upload with a stale url still shows CSV", () => {
    render(
      <DatasetThumbnail
        sourceType="upload"
        thumbnailUrl="https://signed.example/should-not-be-used.png"
        name="r.csv"
      />,
    );
    expect(screen.getByTestId("dataset-thumbnail")).toHaveAttribute("data-kind", "csv");
    expect(screen.queryByTestId("dataset-thumbnail-img")).toBeNull();
  });
});

describe("axe — DatasetThumbnail", () => {
  it("has no violations for the image render", async () => {
    const { container } = render(
      <main>
        <DatasetThumbnail
          sourceType="url"
          thumbnailUrl="https://signed.example/x.png"
          name="Acme CRM"
        />
      </main>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it("has no violations for the CSV render", async () => {
    const { container } = render(
      <main>
        <DatasetThumbnail sourceType="upload" thumbnailUrl={null} name="r.csv" />
      </main>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it("has no violations for the placeholder render", async () => {
    const { container } = render(
      <main>
        <DatasetThumbnail sourceType="url" thumbnailUrl={null} name="New Co" />
      </main>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});
