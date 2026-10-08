/**
 * Tests for {@link TrackedNote} (dataset-ingestion task 9.4, Requirement 6.3).
 *
 * When a checked URL matches an existing dataset, the card shows an "Already
 * tracked" note. Two variants are required:
 * - a *refresh* variant ("adding will refresh it") with a link to the dataset,
 *   shown for a tracked `will_work`/`limited`;
 * - a tracked `wont_work` variant that says the page can't be read right now and
 *   the existing data is unchanged (still linking to the dataset).
 *
 * The variant is asserted through the stable `data-variant` attribute and the
 * link through its `href`, not through colour or exact wording.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeExistingDataset } from "../test/fixtures";
import TrackedNote from "./TrackedNote";

describe("TrackedNote", () => {
  it("shows the refresh variant with a dataset link for a will_work match", () => {
    render(
      <TrackedNote existing={makeExistingDataset({ id: "ds-9", name: "Acme CRM" })} verdict="will_work" />,
    );

    const note = screen.getByTestId("tracked-note");
    expect(note).toHaveAttribute("data-variant", "refresh");
    const link = screen.getByTestId("tracked-link");
    expect(link).toHaveAttribute("href", "/datasets/ds-9");
    expect(link).toHaveTextContent("Acme CRM");
  });

  it("shows the refresh variant for a limited match too", () => {
    render(
      <TrackedNote existing={makeExistingDataset()} verdict="limited" />,
    );
    expect(screen.getByTestId("tracked-note")).toHaveAttribute("data-variant", "refresh");
  });

  it("shows the wont_work variant (existing data unchanged) with the link kept", () => {
    render(
      <TrackedNote existing={makeExistingDataset({ id: "ds-3", name: "Beta Co" })} verdict="wont_work" />,
    );

    const note = screen.getByTestId("tracked-note");
    expect(note).toHaveAttribute("data-variant", "wont_work");
    // The analyst can still open the existing dataset from the note.
    expect(screen.getByTestId("tracked-link")).toHaveAttribute("href", "/datasets/ds-3");
    // The note conveys that nothing changed.
    expect(note).toHaveTextContent(/unchanged/i);
  });

  it("URL-encodes the dataset id in the link", () => {
    render(
      <TrackedNote existing={makeExistingDataset({ id: "ds/with space" })} verdict="will_work" />,
    );
    expect(screen.getByTestId("tracked-link")).toHaveAttribute(
      "href",
      "/datasets/ds%2Fwith%20space",
    );
  });
});
