/**
 * Tests for {@link ChatInput} (guardrailed-chat task 6.3, Requirements 1.1,
 * 1.2, 1.3, 6.1, 6.4).
 *
 * Focused coverage of the input rules and the availability-driven states:
 * Enter vs Shift+Enter, the 1,000-character limit and counter, the
 * empty/whitespace guard, each disabled state and its message, the enabled
 * placeholder, and the 429 "ask again" render. Comprehensive ChatPanel tests
 * are task 6.9. Selectors are data-testid / data-* only (testing.md).
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/ingest";
import ChatInput, {
  ARCHIVED_MESSAGE,
  CHAT_PLACEHOLDER,
  MAX_QUESTION_LENGTH,
  disabledMessage,
  isChatEnabled,
  isSubmittableQuestion,
  type ChatAvailability,
} from "./ChatInput";

/**
 * A controlled harness: ChatInput is presentational, so the parent owns the
 * value. This mirrors how task 6.4 will drive it and lets us type into the
 * textarea and read the submitted question.
 */
function Harness({
  availability = "available",
  pending = false,
  rateLimitError = null,
  onSubmit,
  initialValue = "",
}: {
  availability?: ChatAvailability;
  pending?: boolean;
  rateLimitError?: ApiError | null;
  onSubmit?: (q: string) => void;
  initialValue?: string;
}) {
  const [value, setValue] = useState(initialValue);
  return (
    <ChatInput
      value={value}
      onChange={setValue}
      onSubmit={onSubmit ?? (() => {})}
      availability={availability}
      pending={pending}
      rateLimitError={rateLimitError}
    />
  );
}

describe("ChatInput — input rules (Requirement 6.1, 6.4)", () => {
  it("submits on Enter with the trimmed question", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);

    const textarea = screen.getByTestId("chat-input-textarea");
    await user.type(textarea, "  What are the top complaints?  ");
    await user.keyboard("{Enter}");

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith("What are the top complaints?");
  });

  it("inserts a newline on Shift+Enter and does not submit", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);

    const textarea = screen.getByTestId<HTMLTextAreaElement>("chat-input-textarea");
    await user.type(textarea, "line one");
    await user.keyboard("{Shift>}{Enter}{/Shift}");
    await user.type(textarea, "line two");

    expect(onSubmit).not.toHaveBeenCalled();
    expect(textarea.value).toBe("line one\nline two");
  });

  it("rejects empty and whitespace-only questions (no onSubmit, shows guard)", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);

    // Empty: Enter does nothing but surface the guard.
    await user.click(screen.getByTestId("chat-input-textarea"));
    await user.keyboard("{Enter}");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByTestId("chat-input-empty")).toBeInTheDocument();

    // Whitespace-only: still rejected.
    await user.type(screen.getByTestId("chat-input-textarea"), "    ");
    await user.keyboard("{Enter}");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("keeps the submit button disabled until there is a non-blank question", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const submit = screen.getByTestId("chat-input-submit");

    expect(submit).toBeDisabled();
    await user.type(screen.getByTestId("chat-input-textarea"), "   ");
    expect(submit).toBeDisabled();
    await user.type(screen.getByTestId("chat-input-textarea"), "ok");
    expect(submit).toBeEnabled();
  });

  it("enforces the 1,000-character limit and reflects the count in data-count", async () => {
    const user = userEvent.setup();
    render(<Harness initialValue={"a".repeat(995)} />);

    const textarea = screen.getByTestId<HTMLTextAreaElement>("chat-input-textarea");
    // Try to type 10 more chars; only 5 should land (cap at 1,000).
    await user.type(textarea, "bbbbbbbbbb");

    expect(textarea.value).toHaveLength(MAX_QUESTION_LENGTH);
    const counter = screen.getByTestId("chat-input-counter");
    expect(counter).toHaveAttribute("data-count", String(MAX_QUESTION_LENGTH));
    expect(counter).toHaveAttribute("data-max", String(MAX_QUESTION_LENGTH));
  });

  it("shows the enabled placeholder (Requirement 1.1)", () => {
    render(<Harness availability="available" />);
    expect(screen.getByTestId("chat-input-textarea")).toHaveAttribute(
      "placeholder",
      CHAT_PLACEHOLDER,
    );
  });
});

describe("ChatInput — disabled states and messages (Requirements 1.2, 1.3, 6.2)", () => {
  it("disables with the archived message and no placeholder (Requirement 1.3)", () => {
    render(<Harness availability="archived" />);
    expect(screen.getByTestId("chat-input-textarea")).toBeDisabled();
    const msg = screen.getByTestId("chat-input-disabled-message");
    expect(msg).toHaveTextContent(ARCHIVED_MESSAGE);
    expect(msg).toHaveAttribute("data-availability", "archived");
    expect(screen.getByTestId("chat-input-textarea")).not.toHaveAttribute("placeholder");
  });

  it("disables with an explanatory message when there is no active version (Requirement 1.2)", () => {
    render(<Harness availability="no_active_version" />);
    expect(screen.getByTestId("chat-input-textarea")).toBeDisabled();
    const msg = screen.getByTestId("chat-input-disabled-message");
    expect(msg).toHaveAttribute("data-availability", "no_active_version");
    expect(msg.textContent?.trim().length ?? 0).toBeGreaterThan(0);
  });

  it("disables while an answer is generating with no disabled message (Requirement 6.2)", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness availability="available" pending initialValue="a real question" />);

    const textarea = screen.getByTestId("chat-input-textarea");
    expect(textarea).toBeDisabled();
    expect(screen.queryByTestId("chat-input-disabled-message")).toBeNull();

    // Enter cannot fire onSubmit while pending.
    await user.keyboard("{Enter}");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("stays enabled while refreshing and while a refresh failed (Requirement 1.1)", () => {
    const { rerender } = render(<Harness availability="refreshing" />);
    expect(screen.getByTestId("chat-input-textarea")).toBeEnabled();
    expect(screen.queryByTestId("chat-input-disabled-message")).toBeNull();

    rerender(<Harness availability="refresh_failed" />);
    expect(screen.getByTestId("chat-input-textarea")).toBeEnabled();
    expect(screen.queryByTestId("chat-input-disabled-message")).toBeNull();
  });
});

describe("ChatInput — rate limit (design 'Error Handling')", () => {
  it("disables and shows an ask-again time when rate-limited", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(
      <Harness
        onSubmit={onSubmit}
        rateLimitError={new ApiError(429, "RATE_LIMITED", "Too many requests.", 45)}
        initialValue="a real question"
      />,
    );

    expect(screen.getByTestId("chat-input-textarea")).toBeDisabled();
    const msg = screen.getByTestId("chat-input-rate-limit");
    expect(msg).toHaveTextContent("45 seconds");

    await user.keyboard("{Enter}");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("re-enables once the rate-limit error clears", () => {
    const { rerender } = render(
      <Harness rateLimitError={new ApiError(429, "RATE_LIMITED", "x", 30)} />,
    );
    expect(screen.getByTestId("chat-input-textarea")).toBeDisabled();

    rerender(<Harness rateLimitError={null} />);
    expect(screen.getByTestId("chat-input-textarea")).toBeEnabled();
    expect(screen.queryByTestId("chat-input-rate-limit")).toBeNull();
  });
});

describe("ChatInput — availability helpers", () => {
  it("isChatEnabled is true only for the three usable states", () => {
    expect(isChatEnabled("available")).toBe(true);
    expect(isChatEnabled("refreshing")).toBe(true);
    expect(isChatEnabled("refresh_failed")).toBe(true);
    expect(isChatEnabled("no_active_version")).toBe(false);
    expect(isChatEnabled("archived")).toBe(false);
  });

  it("disabledMessage is set only for the disabled states", () => {
    expect(disabledMessage("archived")).toBe(ARCHIVED_MESSAGE);
    expect(disabledMessage("no_active_version")).not.toBeNull();
    expect(disabledMessage("available")).toBeNull();
    expect(disabledMessage("refreshing")).toBeNull();
    expect(disabledMessage("refresh_failed")).toBeNull();
  });

  it("isSubmittableQuestion matches the empty/whitespace/length rule", () => {
    expect(isSubmittableQuestion("")).toBe(false);
    expect(isSubmittableQuestion("   \n\t ")).toBe(false);
    expect(isSubmittableQuestion("hello")).toBe(true);
    expect(isSubmittableQuestion("a".repeat(MAX_QUESTION_LENGTH))).toBe(true);
    expect(isSubmittableQuestion(` ${"a".repeat(MAX_QUESTION_LENGTH)} `)).toBe(true);
  });
});
