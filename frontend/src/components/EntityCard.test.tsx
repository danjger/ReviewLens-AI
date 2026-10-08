/**
 * Tests for {@link EntityCard} (ingestion-summary task 3.3, Requirement 2.3).
 *
 * The card shows the identified entity name, category, and description, and
 * flags a low-confidence profile with a "Low confidence" chip. When there is no
 * usable entity profile it renders "Not available", consistent with the other
 * metrics panels.
 *
 * Tests target `data-testid`s and the chip's presence/absence rather than any
 * count/date text (testing.md).
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeMetrics } from "../test/fixtures";
import EntityCard from "./EntityCard";

/** A complete, confident entity profile (the review-analysis `entity` shape). */
const FULL_ENTITY = {
  name: "Acme CRM",
  category: "CRM software",
  description: "A customer-relationship management tool for small teams.",
  confidence: "high",
};

describe("EntityCard", () => {
  it("renders the name, category, and description for a full profile", () => {
    render(<EntityCard metrics={makeMetrics({ entity: FULL_ENTITY })} />);

    const card = screen.getByTestId("entity-card");
    expect(card).toHaveAttribute("data-available", "true");
    expect(card).toHaveAttribute("data-confidence", "high");

    expect(screen.getByTestId("entity-name")).toHaveTextContent("Acme CRM");
    expect(screen.getByTestId("entity-category")).toHaveTextContent("CRM software");
    expect(screen.getByTestId("entity-description")).toBeInTheDocument();

    // A confident profile shows no low-confidence chip.
    expect(screen.queryByTestId("entity-low-confidence")).not.toBeInTheDocument();
    expect(screen.queryByTestId("entity-not-available")).not.toBeInTheDocument();
  });

  it("shows the low-confidence chip when confidence is 'low'", () => {
    render(
      <EntityCard metrics={makeMetrics({ entity: { ...FULL_ENTITY, confidence: "low" } })} />,
    );

    const card = screen.getByTestId("entity-card");
    expect(card).toHaveAttribute("data-available", "true");
    expect(card).toHaveAttribute("data-confidence", "low");

    const chip = screen.getByTestId("entity-low-confidence");
    expect(chip).toBeInTheDocument();
    // The lean/flag meaning is carried by text, not colour alone.
    expect(chip).toHaveTextContent(/low confidence/i);

    // The identification is still shown alongside the caveat.
    expect(screen.getByTestId("entity-name")).toHaveTextContent("Acme CRM");
  });

  it("renders a confident card (no chip) even without category/description", () => {
    render(
      <EntityCard
        metrics={makeMetrics({ entity: { name: "Acme CRM", confidence: "high" } })}
      />,
    );

    const card = screen.getByTestId("entity-card");
    expect(card).toHaveAttribute("data-available", "true");
    expect(screen.getByTestId("entity-name")).toHaveTextContent("Acme CRM");
    // Optional fields are simply omitted, not rendered empty.
    expect(screen.queryByTestId("entity-category")).not.toBeInTheDocument();
    expect(screen.queryByTestId("entity-description")).not.toBeInTheDocument();
    expect(screen.queryByTestId("entity-low-confidence")).not.toBeInTheDocument();
  });

  describe("Not available", () => {
    it("shows 'Not available' when the entity block is missing", () => {
      // makeMetrics() has no `entity` key by default.
      render(<EntityCard metrics={makeMetrics()} />);

      const card = screen.getByTestId("entity-card");
      expect(card).toHaveAttribute("data-available", "false");
      expect(screen.getByTestId("entity-not-available")).toHaveTextContent("Not available");
      expect(screen.queryByTestId("entity-name")).not.toBeInTheDocument();
    });

    it("shows 'Not available' when metrics is null", () => {
      render(<EntityCard metrics={null} />);

      expect(screen.getByTestId("entity-card")).toHaveAttribute("data-available", "false");
      expect(screen.getByTestId("entity-not-available")).toBeInTheDocument();
    });

    it("shows 'Not available' when the entity has no usable name", () => {
      render(
        <EntityCard
          metrics={makeMetrics({ entity: { name: "   ", category: "x", confidence: "low" } })}
        />,
      );

      expect(screen.getByTestId("entity-card")).toHaveAttribute("data-available", "false");
      expect(screen.getByTestId("entity-not-available")).toBeInTheDocument();
      expect(screen.queryByTestId("entity-low-confidence")).not.toBeInTheDocument();
    });
  });
});
