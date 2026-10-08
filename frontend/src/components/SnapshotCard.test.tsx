/**
 * Component tests for {@link SnapshotCard} (ingestion-summary task 4.1,
 * Requirements 3.1, 3.2, 3.3).
 *
 * Covers: a URL dataset renders the snapshot loaded through the pre-signed URL
 * (Req 3.1); clicking opens/closes a full-size lightbox (Req 3.2); an image
 * load failure triggers exactly one automatic refetch of the snapshot-url
 * query and then, on a second failure, shows the failure placeholder with the
 * reason (Req 3.3 + design "one automatic refetch"); and an upload dataset
 * (endpoint 404s) renders nothing.
 *
 * The API is mocked with MSW and selectors use data-testid (testing.md). jsdom
 * does not fire `<img>` load/error from a `src` assignment, so image failures
 * are simulated with `fireEvent.error` on the rendered image.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { makeSnapshotUrl } from "../test/fixtures";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";
import SnapshotCard from "./SnapshotCard";

const DATASET_ID = "ds-1";
const SNAPSHOT_PATH = `/api/datasets/${DATASET_ID}/snapshot-url`;

/** Render the card for the standard dataset id. */
function renderCard() {
  return renderWithClient(<SnapshotCard datasetId={DATASET_ID} />);
}

describe("SnapshotCard — URL dataset (Req 3.1)", () => {
  it("renders the snapshot image loaded through the pre-signed URL", async () => {
    const snapshot = makeSnapshotUrl({ url: "https://s3.example/snap.png?sig=1" });
    server.use(
      http.get(SNAPSHOT_PATH, () => HttpResponse.json(snapshot)),
    );

    renderCard();

    const image = await screen.findByTestId("snapshot-image");
    expect(image).toHaveAttribute("src", "https://s3.example/snap.png?sig=1");
    expect(screen.getByTestId("snapshot-card")).toHaveAttribute(
      "data-state",
      "ready",
    );
  });
});

describe("SnapshotCard — lightbox (Req 3.2)", () => {
  it("opens the full-size lightbox on click and closes it again", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(SNAPSHOT_PATH, () => HttpResponse.json(makeSnapshotUrl())),
    );

    renderCard();

    await screen.findByTestId("snapshot-image");
    // Closed by default.
    expect(screen.queryByTestId("snapshot-lightbox")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("snapshot-open"));
    const lightbox = screen.getByTestId("snapshot-lightbox");
    expect(lightbox).toBeInTheDocument();
    expect(screen.getByTestId("snapshot-lightbox-image")).toBeInTheDocument();

    await user.click(screen.getByTestId("snapshot-lightbox-close"));
    expect(screen.queryByTestId("snapshot-lightbox")).not.toBeInTheDocument();
  });
});

describe("SnapshotCard — expired URL refetch (Req 3.3, design 'one refetch')", () => {
  it("refetches the snapshot-url exactly once on image failure, then shows the placeholder on a second failure", async () => {
    let calls = 0;
    // Each fetch mints a distinct URL so we can prove the image re-rendered
    // with the fresh pre-signed URL after the single refetch.
    server.use(
      http.get(SNAPSHOT_PATH, () => {
        calls += 1;
        return HttpResponse.json(
          makeSnapshotUrl({ url: `https://s3.example/snap.png?sig=${calls}` }),
        );
      }),
    );

    renderCard();

    const first = await screen.findByTestId("snapshot-image");
    expect(first).toHaveAttribute("src", "https://s3.example/snap.png?sig=1");
    expect(calls).toBe(1);

    // First image failure → one automatic refetch mints a fresh URL.
    fireEvent.error(first);
    await waitFor(() => expect(calls).toBe(2));
    await waitFor(() =>
      expect(screen.getByTestId("snapshot-image")).toHaveAttribute(
        "src",
        "https://s3.example/snap.png?sig=2",
      ),
    );
    // Still showing the image, not the placeholder, after the single refetch.
    expect(screen.queryByTestId("snapshot-placeholder")).not.toBeInTheDocument();

    // Second failure (fresh URL also failed) → failure placeholder, no further
    // refetch.
    fireEvent.error(screen.getByTestId("snapshot-image"));
    await waitFor(() =>
      expect(screen.getByTestId("snapshot-placeholder")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("snapshot-card")).toHaveAttribute(
      "data-state",
      "failed",
    );
    expect(calls).toBe(2);
  });
});

describe("SnapshotCard — placeholder on request error (Req 3.3)", () => {
  it("shows the failure placeholder with a reason when the snapshot-url request fails", async () => {
    server.use(
      http.get(SNAPSHOT_PATH, () =>
        HttpResponse.json(
          { error: { code: "INTERNAL", message: "snapshot service unavailable" } },
          { status: 500 },
        ),
      ),
    );

    renderCard();

    const placeholder = await screen.findByTestId("snapshot-placeholder");
    expect(placeholder).toBeInTheDocument();
    expect(screen.getByTestId("snapshot-failure-reason")).toHaveTextContent(
      "snapshot service unavailable",
    );
  });
});

describe("SnapshotCard — upload dataset (Req 3.1: no snapshot)", () => {
  it("renders nothing when the endpoint returns 404 (uploads have no snapshot)", async () => {
    server.use(
      http.get(SNAPSHOT_PATH, () =>
        HttpResponse.json(
          { error: { code: "NOT_FOUND", message: "no snapshot for an upload" } },
          { status: 404 },
        ),
      ),
    );

    const { container } = renderCard();

    // Give the query time to settle on the 404, then assert nothing rendered.
    await waitFor(() =>
      expect(screen.queryByTestId("snapshot-card")).not.toBeInTheDocument(),
    );
    expect(screen.queryByTestId("snapshot-image")).not.toBeInTheDocument();
    expect(screen.queryByTestId("snapshot-placeholder")).not.toBeInTheDocument();
    expect(container).toBeEmptyDOMElement();
  });
});
