/**
 * Accessibility (axe) checks for the detail-page summary components
 * (ingestion-summary task 7, design "Testing Strategy / Accessibility":
 * "axe checks run in the component tests").
 *
 * The per-component behavioural coverage lives in each component's own
 * `*.test.tsx` (complete/missing data, warnings, upload-vs-URL, the readiness
 * thresholds and notes, the prediction panel, the live active_version change,
 * and a refresh in progress keeping data available). This file adds the one
 * thing those were missing: automated accessibility assertions. It renders each
 * key component in a representative state (or two, where a state changes the
 * markup — e.g. the chart's table alternative, a low-confidence entity, a
 * warnings list) and asserts axe finds no violations.
 *
 * Notes:
 * - axe's colour-contrast rule does not run under jsdom (jest-axe disables it),
 *   so these checks cover structure/ARIA/roles — exactly the risk areas for the
 *   chart's table alternative, the progressbar on the completeness bar, the
 *   `<time>` elements in the timeline, and the sentiment-with-text indicators.
 * - Components that fetch (SnapshotCard, ReviewsTable, DatasetHeader's rename
 *   hook) are rendered inside a Query client with MSW stubs so the markup is
 *   fully present before axe runs.
 * - A dataset-detail region landmark concern: several components render a bare
 *   fragment, so each is wrapped in a `<main>` to give axe a document landmark
 *   context (region rules otherwise flag content outside a landmark).
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { HttpResponse, http } from "msw";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import type { DatasetDetail } from "../api/datasets";
import {
  makeCitationSnippet,
  makeDatasetDetail,
  makeMetrics,
  makeRefreshMarker,
  makeReviewItem,
  makeReviewsPage,
  makeSnapshotUrl,
  makeViability,
  makeViabilityActual,
  makeStatusEvent,
} from "../test/fixtures";
import { renderWithClient } from "../test/renderWithClient";
import { server } from "../test/server";

import ChatInput from "./ChatInput";
import CitationChip from "./CitationChip";
import CompletenessPanel from "./CompletenessPanel";
import DatasetHeader from "./DatasetHeader";
import DeclineTag from "./DeclineTag";
import EntityCard from "./EntityCard";
import MetricsPanel from "./MetricsPanel";
import PredictionPanel from "./PredictionPanel";
import ProcessingTimeline from "./ProcessingTimeline";
import RatingDistributionChart from "./RatingDistributionChart";
import ReadinessBanner from "./ReadinessBanner";
import RefreshMarker from "./RefreshMarker";
import ReviewsTable from "./ReviewsTable";
import SnapshotCard from "./SnapshotCard";
import StaleExchangeChip from "./StaleExchangeChip";
import ThemesList from "./ThemesList";
import VersionNote from "./VersionNote";

/**
 * Wrap a component in a `<main>` landmark and assert axe finds no violations in
 * the rendered subtree. The landmark gives axe a document context so its
 * region/landmark rules don't flag otherwise-valid fragment markup.
 */
async function expectNoAxeViolations(ui: ReactElement): Promise<void> {
  const { container } = render(<main>{ui}</main>);
  expect(await axe(container)).toHaveNoViolations();
}

describe("axe — MetricsPanel", () => {
  it("has no violations with complete metrics", async () => {
    await expectNoAxeViolations(<MetricsPanel metrics={makeMetrics()} />);
  });

  it("has no violations with missing metrics ('Not available')", async () => {
    await expectNoAxeViolations(<MetricsPanel metrics={null} />);
  });
});

describe("axe — RatingDistributionChart", () => {
  it("has no violations with the table alternative present", async () => {
    // The accessible table alternative is the state most at risk of ARIA/role
    // issues, so this is the important case to axe-check.
    await expectNoAxeViolations(<RatingDistributionChart metrics={makeMetrics()} />);
  });

  it("has no violations in the empty ('Not available') state", async () => {
    await expectNoAxeViolations(
      <RatingDistributionChart metrics={makeMetrics({ rating_distribution: null })} />,
    );
  });
});

describe("axe — EntityCard", () => {
  it("has no violations for a confident entity", async () => {
    await expectNoAxeViolations(
      <EntityCard
        metrics={makeMetrics({
          entity: {
            name: "Acme CRM",
            category: "CRM software",
            description: "A CRM for small teams.",
            confidence: "high",
          },
        })}
      />,
    );
  });

  it("has no violations with the low-confidence chip", async () => {
    await expectNoAxeViolations(
      <EntityCard
        metrics={makeMetrics({
          entity: { name: "Acme CRM", confidence: "low" },
        })}
      />,
    );
  });
});

describe("axe — ThemesList", () => {
  it("has no violations with sentiment-lean (icon-plus-text) items", async () => {
    await expectNoAxeViolations(
      <ThemesList
        metrics={makeMetrics({
          themes: [
            { label: "Ease of setup", mentions: 42, lean: "positive" },
            { label: "Pricing", mentions: 18, lean: "negative" },
            { label: "Support response time", mentions: 7, lean: "neutral" },
          ],
        })}
      />,
    );
  });

  it("has no violations in the empty state", async () => {
    await expectNoAxeViolations(<ThemesList metrics={makeMetrics({ themes: [] })} />);
  });
});

describe("axe — CitationChip (guardrailed-chat task 6.2)", () => {
  it("has no violations with a saved snippet", async () => {
    // The chip is a real button; the popover (role=tooltip, aria-describedby)
    // is the markup most at risk, so axe-check the closed chip here and the
    // open popover below.
    await expectNoAxeViolations(
      <CitationChip id="r_0012" snippet={makeCitationSnippet()} />,
    );
  });

  it("has no violations with the open popover (role=tooltip, aria-describedby)", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <main>
        <CitationChip id="r_0012" snippet={makeCitationSnippet()} />
      </main>,
    );
    await user.tab();
    await screen.findByTestId("citation-popover");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("has no violations in the muted missing-snippet state", async () => {
    await expectNoAxeViolations(<CitationChip id="r_9999" snippet={null} />);
  });
});

describe("axe — DeclineTag (guardrailed-chat task 6.2)", () => {
  it("has no violations for the subtle scope tag", async () => {
    await expectNoAxeViolations(<DeclineTag category="world_knowledge" />);
  });
});

describe("axe — ChatInput (guardrailed-chat task 6.3)", () => {
  it("has no violations when enabled (textarea has a label + placeholder)", async () => {
    await expectNoAxeViolations(
      <ChatInput value="" onChange={() => {}} onSubmit={() => {}} availability="available" />,
    );
  });

  it("has no violations when disabled with a message (archived)", async () => {
    // The disabled message is wired via aria-describedby, so axe-check that the
    // referenced id resolves and the markup is valid.
    await expectNoAxeViolations(
      <ChatInput value="" onChange={() => {}} onSubmit={() => {}} availability="archived" />,
    );
  });
});

describe("axe — VersionNote (guardrailed-chat task 6.3)", () => {
  it("has no violations for the refreshing note", async () => {
    await expectNoAxeViolations(<VersionNote activeVersion={2} state="refreshing" />);
  });
});

describe("axe — RefreshMarker (guardrailed-chat task 6.6)", () => {
  it("has no violations for the completed marker (role=separator)", async () => {
    await expectNoAxeViolations(
      <RefreshMarker marker={makeRefreshMarker({ state: "completed" })} />,
    );
  });

  it("has no violations for the pending marker (spinner role=status)", async () => {
    await expectNoAxeViolations(
      <RefreshMarker
        marker={makeRefreshMarker({
          state: "pending",
          completed_at: null,
          review_count: null,
          previous_review_count: null,
        })}
      />,
    );
  });

  it("has no violations for the failed marker", async () => {
    await expectNoAxeViolations(
      <RefreshMarker marker={makeRefreshMarker({ state: "failed" })} />,
    );
  });
});

describe("axe — StaleExchangeChip (guardrailed-chat task 6.6)", () => {
  it("has no violations with the tooltip wired via aria-describedby", async () => {
    await expectNoAxeViolations(
      <StaleExchangeChip version={1} askedAt="2026-09-02T12:00:00Z" />,
    );
  });
});

describe("axe — CompletenessPanel", () => {
  it("has no violations for a complete URL dataset with a warnings list", async () => {
    // Exercises the progressbar (extracted-vs-reported) and the warnings list
    // together — both markup shapes that axe should sign off on.
    await expectNoAxeViolations(
      <CompletenessPanel
        metrics={makeMetrics({
          review_count: 50,
          reported_total: 200,
          warnings: ["Stopped at page 4: page failed to load"],
        })}
        sourceType="url"
      />,
    );
  });

  it("has no violations for an upload dataset", async () => {
    await expectNoAxeViolations(
      <CompletenessPanel
        metrics={makeMetrics({
          extraction: { method: "upload" },
          reported_total: null,
        })}
        sourceType="upload"
      />,
    );
  });
});

describe("axe — ReadinessBanner", () => {
  it("has no violations in the ready state", async () => {
    await expectNoAxeViolations(
      <ReadinessBanner
        dataset={makeDatasetDetail({
          active_version: 3,
          display_state: "ready",
          metrics: makeMetrics({ warnings: [], review_count: 212 }),
        })}
      />,
    );
  });

  it("has no violations while refreshing (note shown)", async () => {
    await expectNoAxeViolations(
      <ReadinessBanner
        dataset={makeDatasetDetail({
          active_version: 2,
          display_state: "ready_refreshing",
          metrics: makeMetrics(),
        })}
      />,
    );
  });
});

describe("axe — PredictionPanel", () => {
  it("has no violations with a prediction and matching actual", async () => {
    await expectNoAxeViolations(
      <PredictionPanel
        viability={makeViability({
          verdict: "will_work",
          reasons: ["24 reviews found and verified on this page"],
          warnings: ["robots.txt disallows this path"],
          actual: makeViabilityActual({ reviews: 212 }),
        })}
      />,
    );
  });

  it("has no violations with the large-difference highlight", async () => {
    await expectNoAxeViolations(
      <PredictionPanel
        viability={makeViability({
          verdict: "will_work",
          actual: makeViabilityActual({ reviews: 4 }),
        })}
      />,
    );
  });

  it("has no violations in the upload empty state", async () => {
    await expectNoAxeViolations(<PredictionPanel viability={null} />);
  });
});

describe("axe — ProcessingTimeline", () => {
  it("has no violations with a list of events", async () => {
    await expectNoAxeViolations(
      <ProcessingTimeline
        statusDetail={{
          events: [
            makeStatusEvent({ status: "requested", message: "Requested" }),
            makeStatusEvent({ status: null, message: "Fetching page 1 of 10" }),
            makeStatusEvent({ status: "updated", message: "Processed reviews" }),
          ],
        }}
      />,
    );
  });

  it("has no violations in the empty state", async () => {
    await expectNoAxeViolations(<ProcessingTimeline statusDetail={{ events: [] }} />);
  });
});

describe("axe — SnapshotCard (fetches)", () => {
  it("has no violations once the snapshot image has loaded", async () => {
    server.use(
      http.get("/api/datasets/ds-1/snapshot-url", () =>
        HttpResponse.json(makeSnapshotUrl()),
      ),
    );

    const { container } = renderWithClient(
      <main>
        <SnapshotCard datasetId="ds-1" />
      </main>,
    );

    await screen.findByTestId("snapshot-image");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("has no violations in the failure placeholder state", async () => {
    server.use(
      http.get("/api/datasets/ds-1/snapshot-url", () =>
        HttpResponse.json(
          { error: { code: "INTERNAL", message: "snapshot service unavailable" } },
          { status: 500 },
        ),
      ),
    );

    const { container } = renderWithClient(
      <main>
        <SnapshotCard datasetId="ds-1" />
      </main>,
    );

    await screen.findByTestId("snapshot-placeholder");
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("axe — ReviewsTable (fetches)", () => {
  it("has no violations once a page of reviews has rendered", async () => {
    server.use(
      http.get("/api/datasets/ds-1/reviews", () =>
        HttpResponse.json(
          makeReviewsPage([
            makeReviewItem({ id: "r-1", sentiment: "positive", rating: 5 }),
            makeReviewItem({ id: "r-2", sentiment: "neutral", rating: 3 }),
            makeReviewItem({ id: "r-3", sentiment: "negative", rating: 1 }),
          ]),
        ),
      ),
    );

    const { container } = renderWithClient(
      <main>
        <ReviewsTable datasetId="ds-1" />
      </main>,
    );

    await waitFor(() =>
      expect(screen.getAllByTestId("review-row")).toHaveLength(3),
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("axe — DatasetHeader (rename hook)", () => {
  it("has no violations for a URL dataset with resolved redirect hops", async () => {
    const dataset: DatasetDetail = makeDatasetDetail({
      source_type: "url",
      original_url: "https://g2.com/acme",
      final_url: "https://www.g2.com/acme/reviews",
      status_detail: {
        events: [],
        redirects: [
          { url: "https://g2.com/acme", status: 301 },
          { url: "https://www.g2.com/acme/reviews", status: 302, kind: "client" },
        ],
      },
    });

    const { container } = renderWithClient(
      <main>
        <DatasetHeader dataset={dataset} />
      </main>,
    );

    await screen.findByTestId("dataset-header");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("has no violations for an upload dataset header", async () => {
    const dataset: DatasetDetail = makeDatasetDetail({
      source_type: "upload",
      name: "acme-reviews.csv",
      main_url: "acme-reviews.csv",
      original_url: null,
      final_url: null,
      description: "Exported from the CRM.",
    });

    const { container } = renderWithClient(
      <main>
        <DatasetHeader dataset={dataset} />
      </main>,
    );

    await screen.findByTestId("dataset-header");
    expect(await axe(container)).toHaveNoViolations();
  });
});
