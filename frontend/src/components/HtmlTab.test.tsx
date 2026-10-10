/**
 * Component tests for {@link HtmlTab} wired through MSW, an injected uploader,
 * and the mock WebSocket (dataset-ingestion task 21.1, Requirements 8.7, 8.10,
 * 8.14).
 *
 * The HTML tab is a thin producer in front of the shared Check pipeline: it
 * uploads a saved page through the pre-signed mechanism, starts a one-item
 * Check with `POST /ingest/html-checks`, polls it to a verdict, and reuses the
 * URL tab's verdict card and the shared Add flow. These tests cover the task's
 * named cases:
 * - upload progress (Requirement 8.14);
 * - verdict rendering with verified sample reviews (Requirement 8.7);
 * - Add disabled on `wont_work` (Requirements 3.8, 8.14);
 * - the `limited` confirmation (Requirement 3.9);
 * - the "Already tracked …" note when a supplied source URL is tracked
 *   (Requirements 6.3, 8.12, 8.14);
 * - name required — Add stays disabled until a non-empty name is entered
 *   (Requirement 8.14).
 *
 * Per testing.md: the API is mocked with MSW, the WebSocket with
 * `src/test/ws.ts`, and assertions use `data-testid` selectors, never text that
 * includes counts or dates.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Verdict } from "../api/ingest";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import {
  installMockWebSocket,
  uninstallMockWebSocket,
} from "../test/ws";
import HtmlTab from "./HtmlTab";

/** A `will_work` verdict with two verified sample reviews (Requirement 8.7). */
const WILL_WORK: Verdict = {
  verdict: "will_work",
  reasons: ["Reviews found and verified on the uploaded page"],
  warnings: [],
  evidence: {
    reviews_verified: 24,
    reviews_rejected: 0,
    method: "selectors",
    pagination: false,
    reported_total: 24,
    blocker: null,
    locator_confidence: "high",
    page_title: "Acme reviews",
    main_status: 200,
    samples: [
      { text: "Loved it, works perfectly.", rating: 5, date: "2026-01-02" },
      { text: "A bit pricey but good.", rating: 4, date: "2026-01-03" },
    ],
  },
};

const WONT_WORK: Verdict = {
  verdict: "wont_work",
  reasons: ["No reviews could be read from the uploaded page"],
  warnings: [],
  evidence: {
    reviews_verified: 0,
    reviews_rejected: 0,
    method: null,
    pagination: false,
    reported_total: null,
    blocker: "login_wall",
    locator_confidence: null,
    page_title: "Sign in",
    main_status: 200,
    samples: [],
  },
};

const LIMITED: Verdict = {
  verdict: "limited",
  reasons: ["Fewer reviews than the minimum were verified"],
  warnings: [],
  evidence: {
    reviews_verified: 2,
    reviews_rejected: 0,
    method: "ai_direct",
    pagination: false,
    reported_total: null,
    blocker: null,
    locator_confidence: "medium",
    page_title: "Small page",
    main_status: 200,
    samples: [{ text: "It was fine.", rating: 3, date: null }],
  },
};

/** Build the one-item session shape returned by polling. */
function session(options: {
  verdict: Verdict | null;
  state?: string;
  existing?: {
    id: string;
    name: string;
    archived: boolean;
    status: string;
  } | null;
}) {
  return {
    check_id: "chk-html",
    created_at: "2026-01-01T00:00:00Z",
    origin: "new",
    items: [
      {
        item_id: "u1",
        input: "upload:up-1",
        normalized: null,
        final_url: null,
        state: options.state ?? (options.verdict ? "done" : "checking"),
        hops: [],
        verdict: options.verdict,
        existing_dataset: options.existing ?? null,
      },
    ],
  };
}

/**
 * Stub the two producer endpoints (`POST /uploads`, `POST /ingest/html-checks`)
 * and the polling GET; return a getter for the captured Add body.
 */
function stubHtml(options: {
  verdict: Verdict | null;
  existing?: {
    id: string;
    name: string;
    archived: boolean;
    status: string;
  } | null;
  addResults?: unknown;
}): { added: () => Record<string, unknown> | null } {
  let added: Record<string, unknown> | null = null;

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
    http.post("/api/ingest/html-checks", () =>
      HttpResponse.json(
        { check_id: "chk-html", item_id: "u1", state: "pending" },
        { status: 202 },
      ),
    ),
    http.get("/api/ingest/checks/chk-html", () =>
      HttpResponse.json(
        session({ verdict: options.verdict, existing: options.existing }),
      ),
    ),
    http.post("/api/ingest/checks/chk-html/add", async ({ request }) => {
      const body = (await request.json()) as { items: Record<string, unknown>[] };
      added = body.items[0] ?? null;
      return HttpResponse.json(
        options.addResults ?? {
          results: [
            { item_id: "u1", outcome: "created", dataset_id: "ds-1", message: null },
          ],
        },
      );
    }),
  );

  return { added: () => added };
}

/** An uploader that reports two progress ticks, then resolves. */
async function fakeUploader(
  _putUrl: string,
  _file: File,
  onProgress?: (fraction: number) => void,
): Promise<void> {
  onProgress?.(0.5);
  onProgress?.(1);
}

function htmlFile(name = "saved-page.html"): File {
  return new File(["<html><body>reviews</body></html>"], name, {
    type: "text/html",
  });
}

beforeEach(() => {
  installMockWebSocket({ autoOpen: true });
  window.history.pushState(null, "", "/");
});
afterEach(() => {
  uninstallMockWebSocket();
  window.history.pushState(null, "", "/");
});

describe("HtmlTab", () => {
  it("drives the upload progress bar during the pre-signed upload (Requirement 8.14)", async () => {
    const user = userEvent.setup();
    stubHtml({ verdict: WILL_WORK });

    // An uploader we control: report 40%, hold until released, so the progress
    // bar is observable mid-upload.
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

    renderWithClient(
      <HtmlTab
        addNavigation={{ onNavigate: vi.fn() }}
        htmlUpload={{ uploader }}
      />,
    );

    await user.upload(screen.getByTestId("upload-file-input"), htmlFile());

    const progress = await screen.findByTestId("upload-progress");
    await waitFor(() => expect(progress).toHaveAttribute("data-value", "40"));

    release();

    // Once the Check starts and polls, the verdict card appears.
    await screen.findByTestId("verdict-card");
  });

  it("renders the verdict card with verified sample reviews (Requirement 8.7)", async () => {
    const user = userEvent.setup();
    stubHtml({ verdict: WILL_WORK });

    renderWithClient(
      <HtmlTab addNavigation={{ onNavigate: vi.fn() }} htmlUpload={{ uploader: fakeUploader }} />,
    );

    await user.upload(screen.getByTestId("upload-file-input"), htmlFile());

    const card = await screen.findByTestId("verdict-card");
    expect(within(card).getByTestId("verdict-badge")).toHaveAttribute(
      "data-verdict",
      "will_work",
    );

    // The two or three verified samples copied from the uploaded page.
    await user.click(within(card).getByTestId("toggle-samples"));
    expect(within(card).getAllByTestId("sample-review").length).toBeGreaterThanOrEqual(2);

    // The uploaded file name is shown in place of a URL (Requirement 8.10).
    expect(within(card).getByTestId("card-url")).toHaveTextContent("saved-page.html");
  });

  it("keeps Add disabled for a wont_work verdict (Requirements 3.8, 8.14)", async () => {
    const user = userEvent.setup();
    stubHtml({ verdict: WONT_WORK });

    renderWithClient(
      <HtmlTab addNavigation={{ onNavigate: vi.fn() }} htmlUpload={{ uploader: fakeUploader }} />,
    );

    await user.upload(screen.getByTestId("upload-file-input"), htmlFile());
    await screen.findByTestId("verdict-card");

    // Even after a name is typed, a wont_work item is never addable.
    await user.type(screen.getByTestId("html-name-input"), "Blocked page");
    expect(screen.getByTestId("html-add-button")).toBeDisabled();
  });

  it("requires a non-empty name before Add is enabled (Requirement 8.14)", async () => {
    const user = userEvent.setup();
    const captured = stubHtml({ verdict: WILL_WORK });

    renderWithClient(
      <HtmlTab addNavigation={{ onNavigate: vi.fn() }} htmlUpload={{ uploader: fakeUploader }} />,
    );

    await user.upload(screen.getByTestId("upload-file-input"), htmlFile());
    await screen.findByTestId("verdict-card");

    const addButton = screen.getByTestId("html-add-button");
    // Disabled with no name (Requirement 8.14: name is required before Add).
    expect(addButton).toBeDisabled();

    await user.type(screen.getByTestId("html-name-input"), "Acme saved reviews");
    expect(addButton).toBeEnabled();

    await user.click(addButton);

    await waitFor(() => expect(captured.added()).not.toBeNull());
    const body = captured.added() as Record<string, unknown>;
    expect(body.item_id).toBe("u1");
    expect(body.name).toBe("Acme saved reviews");
    // A will_work item is added without the limited confirmation.
    expect(body.confirm_limited).toBe(false);
  });

  it("asks for confirmation before adding a limited verdict (Requirement 3.9)", async () => {
    const user = userEvent.setup();
    const captured = stubHtml({ verdict: LIMITED });

    renderWithClient(
      <HtmlTab addNavigation={{ onNavigate: vi.fn() }} htmlUpload={{ uploader: fakeUploader }} />,
    );

    await user.upload(screen.getByTestId("upload-file-input"), htmlFile());
    await screen.findByTestId("verdict-card");

    await user.type(screen.getByTestId("html-name-input"), "Small saved page");
    await user.click(screen.getByTestId("html-add-button"));

    // The confirmation dialog appears first; nothing is added yet.
    const dialog = await screen.findByTestId("add-confirm-dialog");
    expect(captured.added()).toBeNull();

    await user.click(within(dialog).getByTestId("add-confirm-confirm"));

    await waitFor(() => expect(captured.added()).not.toBeNull());
    const body = captured.added() as Record<string, unknown>;
    // Confirming sends confirm_limited: true for the limited item.
    expect(body.confirm_limited).toBe(true);
  });

  it("shows the 'Already tracked' note when the source URL is tracked (Requirements 6.3, 8.12)", async () => {
    const user = userEvent.setup();
    stubHtml({
      verdict: WILL_WORK,
      existing: {
        id: "ds-existing",
        name: "Acme CRM",
        archived: false,
        status: "updated",
      },
    });

    renderWithClient(
      <HtmlTab addNavigation={{ onNavigate: vi.fn() }} htmlUpload={{ uploader: fakeUploader }} />,
    );

    // Supply a source URL before uploading so it rides along on the check.
    await user.type(
      screen.getByTestId("html-source-url-input"),
      "https://acme.example.com/reviews",
    );
    await user.upload(screen.getByTestId("upload-file-input"), htmlFile());

    const card = await screen.findByTestId("verdict-card");
    const note = within(card).getByTestId("tracked-note");
    expect(note).toHaveAttribute("data-variant", "refresh");
    expect(within(note).getByTestId("tracked-link")).toHaveAttribute(
      "href",
      "/datasets/ds-existing",
    );
  });
});
