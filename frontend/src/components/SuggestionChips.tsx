/**
 * SuggestionChips — the dataset's 3–4 starter questions (guardrailed-chat task
 * 6.5, Requirement 1.4).
 *
 * Shows the dataset's suggested questions as clickable chips. The backend
 * (`app/chat/suggestions_api.py`) builds these from the active version's theme
 * labels using templates, with general fallbacks when there are no themes; the
 * component does not distinguish the two — it just renders whatever strings the
 * {@link useChatSuggestions} hook returns.
 *
 * ## Presentation: empty history vs. collapsed row (design "Frontend (ChatPanel)")
 *
 * Design: the chips are "shown when the history is empty, and above the input
 * as a collapsed row otherwise." So the parent passes `hasHistory`, and this
 * component derives the two presentations from it:
 *
 * - `hasHistory === false` → **empty-history** presentation: the chips are shown
 *   prominently (an onboarding prompt for a fresh dataset).
 * - `hasHistory === true` → **collapsed** presentation: a compact row above the
 *   input, so the suggestions stay reachable without crowding the history.
 *
 * The visual difference is left to CSS, driven by `data-collapsed` on the
 * container; the parent (a future `ChatPanel`, see below) owns the empty-vs-not
 * decision so this component stays presentational.
 *
 * ## Chip-click behavior: prefill (not auto-submit)
 *
 * Clicking a chip calls `onSelect(question)` with the chip's exact text. The
 * chosen behavior is to **prefill the input** (not auto-submit): the parent
 * wires `onSelect` to set {@link StreamingExchange}'s controlled input `value`,
 * so the analyst can read, edit, or extend the suggested question before
 * sending it. That is friendlier than firing a request on a single click — a
 * mis-click doesn't spend an AI call, and a starter question is often a jumping
 * off point the analyst wants to tweak. The component itself is agnostic: it
 * only reports the selected text; whether the parent prefills or submits is the
 * parent's call, documented here as the intended wiring.
 *
 * ## How a future ChatPanel composes this (6.1 + 6.4 + 6.5)
 *
 * The ChatPanel renders, top to bottom: `HistoryPane` (6.1), then — when the
 * history is empty — `SuggestionChips` prominently, else a collapsed
 * `SuggestionChips` row just above the input; then `StreamingExchange` (6.4),
 * which owns the input `value` and `start()`. The panel detects "empty history"
 * from the flattened timeline (no Exchange rows) and passes `hasHistory`
 * accordingly, and passes an `onSelect` that lifts the chosen text into the
 * input value `StreamingExchange` controls (prefill). This component is kept to
 * presentation + the small `useChatSuggestions` hook so that wiring is trivial.
 *
 * ## Testing hooks (testing.md: data-testid / data-* only)
 *
 * The container carries `data-testid="suggestion-chips"` and exposes the
 * collapsed state as `data-collapsed` (`"true"`/`"false"`); each chip carries
 * `data-testid="suggestion-chip"`. Chips are real `<button>`s, so they are
 * keyboard operable (Enter/Space) and focusable for free. Nothing asserts on
 * counts or dates.
 */
import { useChatSuggestions } from "../hooks/useChatSuggestions";

export interface SuggestionChipsProps {
  /** The dataset whose suggestions to fetch, or null before it is known. */
  datasetId: string | null;
  /**
   * Called with a chip's exact question text when the analyst picks it. The
   * parent prefills the chat input with this text (see the module docstring);
   * it does not auto-submit, so the analyst can edit before asking.
   */
  onSelect: (question: string) => void;
  /**
   * Whether the dataset's Q&A history has any Exchanges yet. Drives the
   * empty-vs-collapsed presentation (design "Frontend (ChatPanel)"): `false`
   * shows the chips prominently; `true` shows them as a collapsed row above the
   * input. The parent derives this from the loaded timeline.
   */
  hasHistory: boolean;
}

/** The accessible label for the group of starter-question chips. */
const GROUP_LABEL = "Suggested questions";

export default function SuggestionChips({
  datasetId,
  onSelect,
  hasHistory,
}: SuggestionChipsProps) {
  const { data, isPending, isError } = useChatSuggestions(datasetId);

  // Collapsed (compact row above the input) once there is history to sit above;
  // prominent (expanded) when the history is empty — a fresh-dataset prompt.
  const collapsed = hasHistory;

  // Nothing to show while loading, on error, or when the (defensive) list is
  // empty — the chips are an optional aid, so they fail quiet rather than
  // showing a placeholder or an error row.
  const suggestions = data ?? [];
  if (isPending || isError || suggestions.length === 0) {
    return null;
  }

  return (
    <div
      className="suggestion-chips"
      data-testid="suggestion-chips"
      data-collapsed={collapsed ? "true" : "false"}
      role="group"
      aria-label={GROUP_LABEL}
    >
      {suggestions.map((question) => (
        <button
          key={question}
          type="button"
          className="suggestion-chips__chip"
          data-testid="suggestion-chip"
          onClick={() => onSelect(question)}
        >
          {question}
        </button>
      ))}
    </div>
  );
}
