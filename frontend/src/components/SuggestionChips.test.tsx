/**
 * Focused tests for {@link SuggestionChips} (guardrailed-chat task 6.5,
 * Requirement 1.4).
 *
 * Proves the component fetches the dataset's starter questions over MSW and
 * renders them as accessible chips, that clicking a chip reports its text to
 * `onSelect` (the prefill wiring — see the component docstring), that the
 * empty-vs-collapsed presentation follows the `hasHistory` prop, and that it
 * renders fallback (general) questions the same as theme-based ones. The
 * comprehensive ChatPanel tests are task 6.9. Selectors are data-testid /
 * data-* only (testing.md), so nothing asserts on counts or dates.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { makeSuggestions } from "../test/fixtures";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import SuggestionChips from "./SuggestionChips";

const DATASET_ID = "ds-1";
const SUGGESTIONS_PATH = `/api/datasets/${DATASET_ID}/chat/suggestions`;

/** Install a suggestions handler returning the given `{ suggestions }` body. */
function stubSuggestions(body: { suggestions: string[] }): void {
  server.use(http.get(SUGGESTIONS_PATH, () => HttpResponse.json(body)));
}

describe("SuggestionChips — fetch and render (Req 1.4)", () => {
  it("renders 3–4 chips from the fetched suggestions", async () => {
    stubSuggestions(
      makeSuggestions([
        "What do reviewers say about customer support?",
        "What do reviewers say about pricing?",
        "What do reviewers say about ease of use?",
      ]),
    );

    renderWithClient(
      <SuggestionChips datasetId={DATASET_ID} onSelect={vi.fn()} hasHistory={false} />,
    );

    const chips = await screen.findAllByTestId("suggestion-chip");
    expect(chips).toHaveLength(3);
    expect(chips[0]).toHaveTextContent("customer support");
    // Chips are real buttons → keyboard operable and focusable for free.
    for (const chip of chips) {
      expect(chip.tagName).toBe("BUTTON");
      expect(chip).toHaveAttribute("type", "button");
    }
    // The group is labelled for assistive tech.
    expect(screen.getByTestId("suggestion-chips")).toHaveAttribute("role", "group");
  });

  it("renders general fallback questions the same way (no distinction)", async () => {
    // When no themes are available the backend returns general questions; the
    // component doesn't distinguish — it just renders what it gets.
    stubSuggestions(
      makeSuggestions([
        "What are the most common complaints?",
        "What do reviewers praise most?",
        "How do ratings trend over time?",
        "What would make reviewers happier?",
      ]),
    );

    renderWithClient(
      <SuggestionChips datasetId={DATASET_ID} onSelect={vi.fn()} hasHistory={false} />,
    );

    const chips = await screen.findAllByTestId("suggestion-chip");
    expect(chips).toHaveLength(4);
    expect(chips[0]).toHaveTextContent("most common complaints");
  });

  it("renders nothing when the suggestion list is empty", async () => {
    stubSuggestions({ suggestions: [] });

    renderWithClient(
      <SuggestionChips datasetId={DATASET_ID} onSelect={vi.fn()} hasHistory={false} />,
    );

    // Give the query time to resolve, then assert the container never appears.
    await waitFor(() =>
      expect(screen.queryByTestId("suggestion-chips")).not.toBeInTheDocument(),
    );
    expect(screen.queryAllByTestId("suggestion-chip")).toHaveLength(0);
  });
});

describe("SuggestionChips — chip click prefills via onSelect (Req 1.4)", () => {
  it("calls onSelect with the chip's exact question text", async () => {
    const question = "What do reviewers say about customer support?";
    stubSuggestions(makeSuggestions([question, "What do reviewers say about pricing?"]));
    const onSelect = vi.fn();

    renderWithClient(
      <SuggestionChips datasetId={DATASET_ID} onSelect={onSelect} hasHistory={false} />,
    );

    const chips = await screen.findAllByTestId("suggestion-chip");
    await userEvent.click(chips[0]);

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(question);
  });
});

describe("SuggestionChips — empty vs collapsed presentation (design)", () => {
  it("shows the expanded (prominent) presentation when history is empty", async () => {
    stubSuggestions(makeSuggestions(["q1", "q2", "q3"]));

    renderWithClient(
      <SuggestionChips datasetId={DATASET_ID} onSelect={vi.fn()} hasHistory={false} />,
    );

    const container = await screen.findByTestId("suggestion-chips");
    expect(container).toHaveAttribute("data-collapsed", "false");
  });

  it("shows the collapsed row presentation when there is history", async () => {
    stubSuggestions(makeSuggestions(["q1", "q2", "q3"]));

    renderWithClient(
      <SuggestionChips datasetId={DATASET_ID} onSelect={vi.fn()} hasHistory={true} />,
    );

    const container = await screen.findByTestId("suggestion-chips");
    expect(container).toHaveAttribute("data-collapsed", "true");
  });
});
