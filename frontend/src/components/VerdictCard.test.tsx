/**
 * Smoke tests for {@link VerdictCard}: verdict rendering with samples, the
 * disabled `wont_work` include checkbox, the "Already tracked" notes, and the
 * Retry link. Comprehensive coverage is task 9.4; these guard the building
 * blocks delivered in 9.1.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { axe } from "jest-axe";

import type { CheckItem } from "../api/ingest";
import {
  makeEvidence,
  makeExistingDataset,
  makeItem,
  makeSample,
  makeVerdict,
} from "../test/fixtures";
import VerdictCard from "./VerdictCard";

function renderCard(item: CheckItem, included = false) {
  const onToggleInclude = vi.fn();
  const onRetry = vi.fn();
  render(
    <VerdictCard
      item={item}
      included={included}
      onToggleInclude={onToggleInclude}
      onRetry={onRetry}
    />,
  );
  return { onToggleInclude, onRetry };
}

describe("VerdictCard", () => {
  it("renders the verdict badge, reasons, evidence, and expandable samples", () => {
    renderCard(makeItem());

    expect(screen.getByTestId("verdict-badge")).toHaveAttribute("data-verdict", "will_work");
    expect(screen.getByTestId("verdict-badge")).toHaveTextContent("Will work");
    expect(screen.getAllByTestId("card-reason").length).toBeGreaterThan(0);
    expect(screen.getByTestId("evidence-method")).toHaveAttribute("data-method", "selectors");

    // Samples are collapsed until toggled (Requirement 3.7).
    expect(screen.queryByTestId("sample-list")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("toggle-samples"));
    expect(screen.getAllByTestId("sample-review").length).toBe(2);
  });

  it("disables the include checkbox for a wont_work verdict (Requirement 3.8)", () => {
    const item = makeItem({ verdict: makeVerdict("wont_work", { reasons: ["Not found (404)"] }) });
    renderCard(item);

    const checkbox = screen.getByTestId("include-checkbox");
    expect(checkbox).toBeDisabled();
  });

  it("shows the refresh tracked note with a dataset link (Requirement 6.3)", () => {
    const item = makeItem({ existing_dataset: makeExistingDataset({ id: "ds-9", name: "Acme CRM" }) });
    renderCard(item);

    const note = screen.getByTestId("tracked-note");
    expect(note).toHaveAttribute("data-variant", "refresh");
    expect(screen.getByTestId("tracked-link")).toHaveAttribute("href", "/datasets/ds-9");
  });

  it("shows the wont_work tracked variant (existing data unchanged)", () => {
    const item = makeItem({
      verdict: makeVerdict("wont_work", { reasons: ["Not found (404)"] }),
      existing_dataset: makeExistingDataset(),
    });
    renderCard(item);

    expect(screen.getByTestId("tracked-note")).toHaveAttribute("data-variant", "wont_work");
  });

  it("offers Retry on an errored item and calls back (Requirement 3.10)", () => {
    const item = makeItem({ item_id: "u2", state: "error", verdict: null });
    const { onRetry } = renderCard(item);

    fireEvent.click(screen.getByTestId("retry-link"));
    expect(onRetry).toHaveBeenCalledWith("u2");
  });

  it("offers Retry on a timed-out wont_work item", () => {
    const item = makeItem({
      verdict: makeVerdict("wont_work", { reasons: ["Page took too long to load"] }),
    });
    const { onRetry } = renderCard(item);

    fireEvent.click(screen.getByTestId("retry-link"));
    expect(onRetry).toHaveBeenCalledWith("u1");
  });

  it("shows progress while the item is still checking", () => {
    const item = makeItem({ state: "checking", verdict: null });
    renderCard(item);
    expect(screen.getByTestId("card-progress")).toHaveTextContent("Checking…");
  });

  it("shows the queued progress while the item is still pending", () => {
    renderCard(makeItem({ state: "pending", verdict: null }));
    expect(screen.getByTestId("card-progress")).toHaveTextContent("Queued…");
  });

  it("renders the limited badge and leaves its include checkbox enabled (Requirement 3.8)", () => {
    const item = makeItem({ verdict: makeVerdict("limited", { reasons: ["Only 2 reviews found"] }) });
    renderCard(item, true);

    expect(screen.getByTestId("verdict-badge")).toHaveAttribute("data-verdict", "limited");
    // Only wont_work is disabled; limited can be included (on by default upstream).
    const checkbox = screen.getByTestId("include-checkbox");
    expect(checkbox).not.toBeDisabled();
    expect(checkbox).toBeChecked();
  });

  it("renders the will_work include checkbox as enabled", () => {
    renderCard(makeItem(), true);
    const checkbox = screen.getByTestId("include-checkbox");
    expect(checkbox).not.toBeDisabled();
    expect(checkbox).toBeChecked();
  });

  it("toggling the include checkbox reports the change upward (Requirement 3.8)", () => {
    const { onToggleInclude } = renderCard(makeItem({ item_id: "u5" }), false);
    fireEvent.click(screen.getByTestId("include-checkbox"));
    expect(onToggleInclude).toHaveBeenCalledWith("u5", true);
  });

  it("renders warnings, including the robots.txt disallow (Requirement 3.12)", () => {
    const item = makeItem({
      verdict: makeVerdict("will_work", {
        warnings: ["This page is disallowed for crawlers by robots.txt"],
      }),
    });
    renderCard(item);

    const warnings = screen.getAllByTestId("card-warning");
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toHaveTextContent(/robots\.txt/i);
  });

  it("shows the blocker in the evidence for a blocked wont_work page", () => {
    const item = makeItem({
      verdict: makeVerdict("wont_work", {
        reasons: ["A login wall was found"],
        evidence: makeEvidence({ reviews_verified: 0, blocker: "login_wall", method: null, samples: [] }),
      }),
    });
    renderCard(item);
    expect(screen.getByTestId("evidence-blocker")).toHaveAttribute("data-blocker", "login_wall");
  });

  it("expands up to three sample reviews (Requirement 3.7)", () => {
    const item = makeItem({
      verdict: makeVerdict("will_work", {
        evidence: makeEvidence({
          samples: [
            makeSample({ text: "One" }),
            makeSample({ text: "Two" }),
            makeSample({ text: "Three" }),
          ],
        }),
      }),
    });
    renderCard(item);
    fireEvent.click(screen.getByTestId("toggle-samples"));
    expect(screen.getAllByTestId("sample-review")).toHaveLength(3);
  });

  it("renders an invalid line's message instead of a verdict (Requirement 1.2)", () => {
    const item = makeItem({
      state: "invalid",
      verdict: null,
      message: "This line is not a valid http or https URL.",
    });
    renderCard(item);

    expect(screen.getByTestId("card-invalid")).toHaveTextContent(/not a valid/i);
    expect(screen.queryByTestId("verdict-badge")).not.toBeInTheDocument();
    // An invalid line cannot be included.
    expect(screen.getByTestId("include-checkbox")).toBeDisabled();
  });

  it("renders an in-batch duplicate line's message (Requirement 1.4)", () => {
    const item = makeItem({
      state: "duplicate_in_batch",
      verdict: null,
      message: "Duplicate of another URL in this submission.",
    });
    renderCard(item);

    expect(screen.getByTestId("card-duplicate")).toHaveTextContent(/duplicate/i);
    expect(screen.queryByTestId("verdict-badge")).not.toBeInTheDocument();
  });

  it("does not offer Retry on a healthy will_work item", () => {
    renderCard(makeItem());
    expect(screen.queryByTestId("retry-link")).not.toBeInTheDocument();
  });

  it("disables the Retry link while a retry is in flight", () => {
    const item = makeItem({ item_id: "u2", state: "error", verdict: null });
    const onToggleInclude = vi.fn();
    const onRetry = vi.fn();
    render(
      <VerdictCard
        item={item}
        included={false}
        onToggleInclude={onToggleInclude}
        onRetry={onRetry}
        retrying
      />,
    );
    expect(screen.getByTestId("retry-link")).toBeDisabled();
  });

  it("shows a Clear button for a wont_work result and calls onDismiss", () => {
    const item = makeItem({ verdict: makeVerdict("wont_work", { reasons: ["Status 403"] }) });
    const onDismiss = vi.fn();
    render(
      <VerdictCard
        item={item}
        included={false}
        onToggleInclude={vi.fn()}
        onRetry={vi.fn()}
        onDismiss={onDismiss}
      />,
    );
    const clear = screen.getByTestId("dismiss-link");
    fireEvent.click(clear);
    expect(onDismiss).toHaveBeenCalledWith(item.item_id);
  });

  it("does not show Clear for a viable (will_work) result", () => {
    render(
      <VerdictCard
        item={makeItem()}
        included
        onToggleInclude={vi.fn()}
        onRetry={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("dismiss-link")).not.toBeInTheDocument();
  });

  it("does not show Clear when no onDismiss handler is provided", () => {
    const item = makeItem({ verdict: makeVerdict("wont_work", { reasons: ["Status 403"] }) });
    render(
      <VerdictCard item={item} included={false} onToggleInclude={vi.fn()} onRetry={vi.fn()} />,
    );
    expect(screen.queryByTestId("dismiss-link")).not.toBeInTheDocument();
  });

});


describe("axe — VerdictCard (task 11.3)", () => {
  it("has no violations for a wont_work card with the Clear action", async () => {
    const item = makeItem({ verdict: makeVerdict("wont_work", { reasons: ["Status 403"] }) });
    const { container } = render(
      <ul>
        <VerdictCard
          item={item}
          included={false}
          onToggleInclude={vi.fn()}
          onRetry={vi.fn()}
          onDismiss={vi.fn()}
        />
      </ul>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});
