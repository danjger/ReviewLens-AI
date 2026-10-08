/**
 * Integration tests for {@link UploadTab} wired through MSW and an injected
 * uploader (dataset-ingestion task 9.3).
 *
 * These are smoke/component tests of the end-to-end Upload flow (the exhaustive
 * cases are task 9.4):
 * - choosing a file starts the upload and the progress bar reflects the
 *   injected uploader's progress (Requirement 7.1);
 * - the preview renders the detected mapping and the over-limit note using the
 *   keep rule (Requirement 7.6);
 * - editing the column mapper changes the mapping sent on submit (Requirement
 *   7.2);
 * - submit is gated on a required non-empty name and a mapped text column
 *   (Requirement 7.4);
 * - a successful submit navigates to the new dataset's detail page
 *   (Requirements 7.4, 5.4).
 *
 * The direct S3 PUT is replaced with an injected `uploader` that drives
 * progress to 100% so the component logic is tested without a real
 * `XMLHttpRequest`; the three JSON endpoints are stubbed with MSW.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UploadPreview } from "../api/uploads";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import UploadTab from "./UploadTab";

const PREVIEW: UploadPreview = {
  columns: ["Review", "Stars", "When"],
  suggested_mapping: { text: "Review", rating: "Stars", date: "When" },
  sample_rows: [
    { Review: "Great product", Stars: "5", When: "2026-01-02" },
    { Review: "A bit pricey", Stars: "4", When: "2026-01-03" },
  ],
  usable_rows: 1200,
  will_keep: 1000,
  keep_rule: "most_recent_by_date",
};

/** Stub the three upload JSON endpoints; capture the submit body. */
function stubUpload(options: {
  preview?: UploadPreview;
  submitStatus?: number;
  submitBody?: unknown;
} = {}): { submitted: () => Record<string, unknown> | null } {
  const preview = options.preview ?? PREVIEW;
  let submitted: Record<string, unknown> | null = null;

  server.use(
    http.post("/api/uploads", () =>
      HttpResponse.json(
        {
          upload_id: "up-1",
          put_url: "https://s3.example.test/uploads/up-1/file?sig=abc",
          expires_at: "2026-01-01T00:15:00Z",
        },
        { status: 201 },
      ),
    ),
    http.post("/api/uploads/up-1/preview", () => HttpResponse.json(preview, { status: 200 })),
    http.post("/api/datasets/upload", async ({ request }) => {
      submitted = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(options.submitBody ?? { id: "ds-99" }, {
        status: options.submitStatus ?? 201,
      });
    }),
  );

  return { submitted: () => submitted };
}

/** An uploader that reports a couple of progress ticks, then resolves. */
async function fakeUploader(
  _putUrl: string,
  _file: File,
  onProgress?: (fraction: number) => void,
): Promise<void> {
  onProgress?.(0.5);
  onProgress?.(1);
}

function csvFile(name = "reviews.csv"): File {
  return new File(["Review,Stars,When\nGreat,5,2026-01-02"], name, { type: "text/csv" });
}

beforeEach(() => window.history.pushState(null, "", "/"));
afterEach(() => window.history.pushState(null, "", "/"));

describe("UploadTab", () => {
  it("uploads a file, previews it, maps columns, and navigates on submit", async () => {
    const user = userEvent.setup();
    const captured = stubUpload();
    const onNavigate = vi.fn();

    renderWithClient(
      <UploadTab upload={{ onNavigate, uploader: fakeUploader }} />,
    );

    // Choose a file → the pipeline runs and the preview appears.
    await user.upload(screen.getByTestId("upload-file-input"), csvFile());

    const form = await screen.findByTestId("upload-form");

    // The detected mapping is reflected in the preview and the mapper.
    expect(within(form).getByTestId("upload-map-text")).toHaveValue("Review");
    expect(within(form).getByTestId("upload-map-rating")).toHaveValue("Stars");

    // The over-limit note uses the keep rule (assert via data-*, not counts).
    const note = within(form).getByTestId("upload-over-limit-note");
    expect(note).toHaveAttribute("data-over-limit", "true");
    expect(note).toHaveAttribute("data-keep-rule", "most_recent_by_date");
    expect(note).toHaveAttribute("data-usable-rows", "1200");
    expect(note).toHaveAttribute("data-will-keep", "1000");

    // Submit is disabled until a name is given (Requirement 7.4).
    const submitButton = within(form).getByTestId("upload-submit-button");
    expect(submitButton).toBeDisabled();

    await user.type(screen.getByTestId("upload-name-input"), "Acme Reviews");
    expect(submitButton).toBeEnabled();

    await user.click(submitButton);

    await waitFor(() => expect(captured.submitted()).not.toBeNull());
    const body = captured.submitted() as Record<string, unknown>;
    expect(body.upload_id).toBe("up-1");
    expect(body.name).toBe("Acme Reviews");
    expect(body.mapping).toMatchObject({ text: "Review", rating: "Stars", date: "When" });

    await waitFor(() => expect(onNavigate).toHaveBeenCalledWith("/datasets/ds-99"));
  });

  it("drives the progress bar from the uploader during the direct upload", async () => {
    const user = userEvent.setup();
    stubUpload();

    // An uploader we control: report 40%, then hold until released, so the
    // progress bar is observable mid-upload.
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const uploader = async (
      _u: string,
      _f: File,
      onProgress?: (fraction: number) => void,
    ): Promise<void> => {
      onProgress?.(0.4);
      await held;
      onProgress?.(1);
    };

    renderWithClient(<UploadTab upload={{ uploader, onNavigate: vi.fn() }} />);
    await user.upload(screen.getByTestId("upload-file-input"), csvFile());

    const progress = await screen.findByTestId("upload-progress");
    await waitFor(() => expect(progress).toHaveAttribute("data-value", "40"));

    release();
    // After release, the preview form appears.
    await screen.findByTestId("upload-form");
  });

  it("edits the mapping and sends the corrected mapping on submit (Requirement 7.2)", async () => {
    const user = userEvent.setup();
    const captured = stubUpload();

    renderWithClient(<UploadTab upload={{ onNavigate: vi.fn(), uploader: fakeUploader }} />);
    await user.upload(screen.getByTestId("upload-file-input"), csvFile());
    await screen.findByTestId("upload-form");

    // Unmap rating and remap text to a different column.
    await user.selectOptions(screen.getByTestId("upload-map-rating"), "");
    await user.selectOptions(screen.getByTestId("upload-map-text"), "When");

    await user.type(screen.getByTestId("upload-name-input"), "Edited Mapping");
    await user.click(screen.getByTestId("upload-submit-button"));

    await waitFor(() => expect(captured.submitted()).not.toBeNull());
    const body = captured.submitted() as { mapping: Record<string, string> };
    expect(body.mapping.text).toBe("When");
    expect(body.mapping.rating).toBeUndefined();
  });

  it("does not render the over-limit note when the file fits", async () => {
    const user = userEvent.setup();
    stubUpload({
      preview: {
        ...PREVIEW,
        usable_rows: 20,
        will_keep: 20,
        keep_rule: "first_in_file",
      },
    });

    renderWithClient(<UploadTab upload={{ onNavigate: vi.fn(), uploader: fakeUploader }} />);
    await user.upload(screen.getByTestId("upload-file-input"), csvFile());
    await screen.findByTestId("upload-form");

    expect(screen.queryByTestId("upload-over-limit-note")).not.toBeInTheDocument();
  });

  it("prompts a re-upload when the preview 422s (staged object deleted, Req 7.3)", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/uploads", () =>
        HttpResponse.json(
          {
            upload_id: "up-1",
            put_url: "https://s3.example.test/uploads/up-1/file?sig=abc",
            expires_at: "2026-01-01T00:15:00Z",
          },
          { status: 201 },
        ),
      ),
      http.post("/api/uploads/up-1/preview", () =>
        HttpResponse.json(
          { error: { code: "VALIDATION_ERROR", message: "No review text column was found." } },
          { status: 422 },
        ),
      ),
    );

    renderWithClient(<UploadTab upload={{ onNavigate: vi.fn(), uploader: fakeUploader }} />);
    await user.upload(screen.getByTestId("upload-file-input"), csvFile());

    const note = await screen.findByTestId("upload-reupload-note");
    expect(note).toHaveTextContent("No review text column was found.");
    expect(screen.queryByTestId("upload-form")).not.toBeInTheDocument();
  });
});
