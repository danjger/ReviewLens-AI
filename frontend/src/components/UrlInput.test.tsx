/**
 * Tests for {@link UrlInput}: line parsing, the 10-line note, and blocking a
 * second submission while a Check is running (Requirements 1.1, 1.5).
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import UrlInput, { parseLines } from "./UrlInput";

describe("parseLines", () => {
  it("trims and drops blank lines", () => {
    expect(parseLines("  https://a.com \n\n https://b.com \n")).toEqual([
      "https://a.com",
      "https://b.com",
    ]);
  });
});

describe("UrlInput", () => {
  it("submits only the first 10 lines and shows the over-limit note", () => {
    const onSubmit = vi.fn();
    const eleven = Array.from({ length: 11 }, (_, i) => `https://e.com/${i}`).join("\n");
    render(<UrlInput value={eleven} onChange={() => {}} onSubmit={onSubmit} running={false} />);

    expect(screen.getByTestId("url-input-over-limit")).toBeInTheDocument();
    fireEvent.submit(screen.getByTestId("url-input-form"));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0]).toHaveLength(10);
  });

  it("blocks a second submission while running (Requirement 1.5)", () => {
    const onSubmit = vi.fn();
    render(
      <UrlInput value="https://a.com" onChange={() => {}} onSubmit={onSubmit} running={true} />,
    );

    expect(screen.getByTestId("check-button")).toBeDisabled();
    fireEvent.submit(screen.getByTestId("url-input-form"));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("disables the button when there is no input", () => {
    render(<UrlInput value="   " onChange={() => {}} onSubmit={vi.fn()} running={false} />);
    expect(screen.getByTestId("check-button")).toBeDisabled();
  });

  it("does not show the over-limit note at exactly 10 lines and submits all 10", () => {
    const onSubmit = vi.fn();
    const ten = Array.from({ length: 10 }, (_, i) => `https://e.com/${i}`).join("\n");
    render(<UrlInput value={ten} onChange={() => {}} onSubmit={onSubmit} running={false} />);

    // 10 is the maximum, not over the limit.
    expect(screen.queryByTestId("url-input-over-limit")).not.toBeInTheDocument();
    fireEvent.submit(screen.getByTestId("url-input-form"));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0]).toHaveLength(10);
  });

  it("submits a single line without the over-limit note", () => {
    const onSubmit = vi.fn();
    render(
      <UrlInput value="https://a.com" onChange={() => {}} onSubmit={onSubmit} running={false} />,
    );

    expect(screen.queryByTestId("url-input-over-limit")).not.toBeInTheDocument();
    fireEvent.submit(screen.getByTestId("url-input-form"));
    expect(onSubmit).toHaveBeenCalledWith(["https://a.com"]);
  });

  it("does not submit when the input is only blank lines", () => {
    const onSubmit = vi.fn();
    render(<UrlInput value={"\n   \n\n"} onChange={() => {}} onSubmit={onSubmit} running={false} />);

    expect(screen.getByTestId("check-button")).toBeDisabled();
    fireEvent.submit(screen.getByTestId("url-input-form"));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("parses interior blank lines out of a multi-line paste", () => {
    const onSubmit = vi.fn();
    render(
      <UrlInput
        value={"https://a.com\n\n  \nhttps://b.com\n"}
        onChange={() => {}}
        onSubmit={onSubmit}
        running={false}
      />,
    );
    fireEvent.submit(screen.getByTestId("url-input-form"));
    expect(onSubmit).toHaveBeenCalledWith(["https://a.com", "https://b.com"]);
  });
});

describe("parseLines (extra cases)", () => {
  it("keeps all lines (parsing does not cap at 10; the form slices)", () => {
    const eleven = Array.from({ length: 11 }, (_, i) => `https://e.com/${i}`).join("\n");
    expect(parseLines(eleven)).toHaveLength(11);
  });

  it("returns an empty array for all-blank input", () => {
    expect(parseLines("\n   \n\t\n")).toEqual([]);
  });
});
