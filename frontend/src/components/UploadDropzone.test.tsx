/**
 * Component tests for {@link UploadDropzone} (dataset-ingestion task 9.3).
 *
 * Covers the file picker's contract: it reports an accepted `.csv`/`.tsv` file
 * upward, rejects a wrong extension with a message before any upload starts
 * (Requirement 7.1), and shows the chosen file name (Requirement 7.5).
 */
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import UploadDropzone from "./UploadDropzone";

function csvFile(name = "reviews.csv"): File {
  return new File(["review\nGreat product"], name, { type: "text/csv" });
}

describe("UploadDropzone", () => {
  it("reports a chosen .csv file upward (Requirement 7.1)", async () => {
    const user = userEvent.setup();
    const onFile = vi.fn();
    render(<UploadDropzone onFile={onFile} />);

    const input = screen.getByTestId("upload-file-input") as HTMLInputElement;
    await user.upload(input, csvFile());

    expect(onFile).toHaveBeenCalledTimes(1);
    expect(onFile.mock.calls[0][0].name).toBe("reviews.csv");
  });

  it("accepts a dropped .tsv file", () => {
    const onFile = vi.fn();
    render(<UploadDropzone onFile={onFile} />);

    const file = new File(["review\tdate"], "reviews.tsv", { type: "text/tab-separated-values" });
    fireEvent.drop(screen.getByTestId("upload-dropzone-zone"), {
      dataTransfer: { files: [file] },
    });

    expect(onFile).toHaveBeenCalledTimes(1);
    expect(onFile.mock.calls[0][0].name).toBe("reviews.tsv");
  });

  it("rejects a wrong extension with a message and does not report it", () => {
    const onFile = vi.fn();
    render(<UploadDropzone onFile={onFile} />);

    // The native `accept` filter would normally stop a `.json` from reaching
    // the input, so drop one directly to exercise the component's own guard
    // (the backend applies the authoritative check regardless).
    fireEvent.drop(screen.getByTestId("upload-dropzone-zone"), {
      dataTransfer: {
        files: [new File(["{}"], "data.json", { type: "application/json" })],
      },
    });

    expect(onFile).not.toHaveBeenCalled();
    expect(screen.getByTestId("upload-dropzone-error")).toBeInTheDocument();
  });

  it("shows the chosen file name (Requirement 7.5)", () => {
    render(<UploadDropzone onFile={vi.fn()} fileName="my-reviews.csv" />);
    expect(screen.getByTestId("upload-filename")).toHaveTextContent("my-reviews.csv");
  });
});
