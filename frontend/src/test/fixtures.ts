/**
 * Test fixtures for Check Session shapes.
 *
 * Small builders that produce the exact JSON shape the backend returns from
 * `GET /ingest/checks/{id}` (see `app/ingestion/api.py` `_item_to_view` and the
 * verdict/evidence `to_dict` methods), so component tests can assert against
 * realistic data without hand-writing it each time.
 */
import type {
  AddOutcome,
  AddResult,
  CheckItem,
  CheckSession,
  Evidence,
  ExistingDataset,
  Sample,
  Verdict,
  VerdictLabel,
} from "../api/ingest";

export function makeSample(overrides: Partial<Sample> = {}): Sample {
  return { text: "Setup took an afternoon and support was great.", rating: 5, date: "2026-09-02", ...overrides };
}

export function makeEvidence(overrides: Partial<Evidence> = {}): Evidence {
  return {
    reviews_verified: 24,
    reviews_rejected: 0,
    method: "selectors",
    pagination: true,
    reported_total: 1540,
    blocker: null,
    locator_confidence: "high",
    page_title: "Acme CRM Reviews",
    main_status: 200,
    samples: [makeSample(), makeSample({ text: "A bit pricey but worth it.", rating: 4 })],
    ...overrides,
  };
}

export function makeVerdict(
  verdict: VerdictLabel = "will_work",
  overrides: Partial<Verdict> = {},
): Verdict {
  const base: Verdict = {
    verdict,
    reasons: ["24 reviews found and verified on this page", "Next page link found"],
    warnings: [],
    evidence: makeEvidence(),
  };
  return { ...base, ...overrides };
}

export function makeExistingDataset(overrides: Partial<ExistingDataset> = {}): ExistingDataset {
  return { id: "ds-1", name: "Acme CRM", archived: false, status: "updated", ...overrides };
}

export function makeItem(overrides: Partial<CheckItem> = {}): CheckItem {
  return {
    item_id: "u1",
    input: "https://example.com/acme-crm/reviews",
    normalized: "https://example.com/acme-crm/reviews",
    final_url: "https://example.com/acme-crm/reviews",
    state: "done",
    hops: [],
    verdict: makeVerdict(),
    existing_dataset: null,
    ...overrides,
  };
}

export function makeSession(items: CheckItem[], overrides: Partial<CheckSession> = {}): CheckSession {
  return {
    check_id: "check-1",
    created_at: "2026-09-02T00:00:00Z",
    origin: "new",
    items,
    ...overrides,
  };
}

export function makeAddResult(
  outcome: AddOutcome = "created",
  overrides: Partial<AddResult> = {},
): AddResult {
  const withDataset: AddOutcome[] = [
    "created",
    "refreshed",
    "restored_and_refreshed",
    "already_refreshing",
  ];
  return {
    item_id: "u1",
    outcome,
    dataset_id: withDataset.includes(outcome) ? "ds-1" : null,
    message: null,
    ...overrides,
  };
}

// ── Dataset Library fixtures (dataset-library task 6) ───────────────────────

import type {
  Dataset,
  DatasetDetail,
  DatasetVersion,
  ReviewItem,
  ReviewsPage,
  SnapshotUrl,
  StatusEvent,
  Viability,
  ViabilityActual,
} from "../api/datasets";

/**
 * Build a tracked-dataset list row (`GET /datasets`). Defaults to a healthy
 * `ready` URL dataset; override fields to exercise the `display_state` badges,
 * refresh states, archived rows, upload datasets, etc.
 */
export function makeDataset(overrides: Partial<Dataset> = {}): Dataset {
  return {
    id: "ds-1",
    name: "Acme CRM",
    main_url: "https://g2.com/products/acme/reviews",
    platform: "g2",
    status: "updated",
    display_state: "ready",
    last_message: "Processed 212 reviews",
    review_count: 212,
    data_version: 3,
    active_version: 3,
    requested_at: "2026-09-01T00:00:00Z",
    last_refreshed_at: "2026-09-02T00:00:00Z",
    archived_at: null,
    refresh_check_id: null,
    ...overrides,
  };
}

/**
 * Build a `GET /datasets/{id}/snapshot-url` response (backend
 * `summary.SnapshotUrl`): a short-lived pre-signed URL for a URL dataset's PNG
 * snapshot. Override `url`/`version` to exercise an active-version change.
 */
export function makeSnapshotUrl(
  overrides: Partial<SnapshotUrl> = {},
): SnapshotUrl {
  return {
    url: "https://s3.example/snapshot/v3.png?sig=abc",
    expires_at: "2026-09-02T20:25:00Z",
    version: 3,
    ...overrides,
  };
}

export function makeDatasetVersion(
  overrides: Partial<DatasetVersion> = {},
): DatasetVersion {
  return {
    version: 3,
    status: "updated",
    review_count: 212,
    completed_at: "2026-09-02T00:00:00Z",
    ...overrides,
  };
}

/**
 * Build a `status_detail.viability.actual` block (backend
 * `completion.ActualResult.as_dict`): the actual processing result recorded
 * next to the Viability Check prediction (Requirement 6.4 / 7.1).
 */
export function makeViabilityActual(
  overrides: Partial<ViabilityActual> = {},
): ViabilityActual {
  return { reviews: 212, pages: 10, method: "selectors", fallbacks: 0, ...overrides };
}

/**
 * Build a `status_detail.viability` block: the stored Viability {@link Verdict}
 * (`app/ingestion/service.py`) with the optional `actual` result appended by
 * the completion stage. Defaults to a healthy `will_work` prediction whose
 * actual result matches (so no large-difference highlight); override `verdict`,
 * `evidence.reviews_verified` (the detected count), and `actual` to exercise
 * Requirement 7.2's highlight cases.
 */
export function makeViability(overrides: Partial<Viability> = {}): Viability {
  return {
    ...makeVerdict(),
    actual: makeViabilityActual(),
    ...overrides,
  };
}

/**
 * Build one `status_detail.events` entry in the exact shape the backend writes
 * (`app/db/status.py::_build_event`): `status` (the lifecycle status, or null
 * for a progress-only `log_event`), `at` (ISO-8601 UTC), `message`, and `data`.
 * Used by the {@link ProcessingTimeline} tests (ingestion-summary task 6.1).
 */
export function makeStatusEvent(overrides: Partial<StatusEvent> = {}): StatusEvent {
  return {
    status: "processing",
    at: "2026-09-02T20:20:00Z",
    message: "Fetching page 1 of 10",
    data: {},
    ...overrides,
  };
}

export function makeDatasetDetail(
  overrides: Partial<DatasetDetail> = {},
): DatasetDetail {
  return {
    ...makeDataset(),
    source_type: "url",
    original_url: "https://g2.com/products/acme/reviews",
    final_url: "https://g2.com/products/acme/reviews",
    page_title: "Acme CRM Reviews",
    metrics: makeMetrics(),
    description: null,
    status_detail: { events: [], redirects: [], viability: makeViability() },
    versions: [makeDatasetVersion()],
    ...overrides,
  };
}

/**
 * Build an active-version `metrics` dict in the shape the review-analysis
 * metrics stage produces (`app/handlers/metrics.py::compute_metrics`), so the
 * detail-page component tests exercise the real field names.
 *
 * Defaults to a complete dataset; override a field with `null` (or delete it
 * from the returned object) to exercise the "Not available" handling.
 */
export function makeMetrics(
  overrides: Partial<Record<string, unknown>> = {},
): Record<string, unknown> {
  return {
    review_count: 212,
    reported_total: 1540,
    pages_captured: 10,
    skipped: 0,
    avg_rating: 3.8,
    rating_distribution: { "1": 10, "2": 12, "3": 30, "4": 60, "5": 100 },
    date_range: { min: "2024-01-05", max: "2026-09-02" },
    sentiment: { positive: 120, neutral: 50, negative: 42 },
    themes: [],
    extraction: {
      method: "selectors",
      pages_by_selectors: 10,
      pages_by_ai: 0,
      locator_discarded: 0,
      structured_agreement: null,
    },
    warnings: [],
    duration_ms: 1234,
    ...overrides,
  };
}

// ── Reviews table fixtures (ingestion-summary task 5) ───────────────────────

/**
 * Build one review in the exact shape the reviews endpoint returns (backend
 * `app/handlers/metrics.py::compute_metrics` `reviews_payload`): `id`, `text`,
 * `rating`, `date`, `author`, `title`, `sentiment`, and `source_page`. Defaults
 * to a short positive review; override `text` (e.g. a long string) to exercise
 * the truncate/expand toggle, or null `rating`/`date`/`author`/`title` to
 * exercise the empty-cell handling.
 */
export function makeReviewItem(overrides: Partial<ReviewItem> = {}): ReviewItem {
  return {
    id: "r-1",
    text: "Setup took an afternoon and support was great.",
    rating: 5,
    date: "2026-09-02",
    author: "Alex",
    title: "Great onboarding",
    sentiment: "positive",
    source_page: 1,
    ...overrides,
  };
}

/**
 * Build a `GET /datasets/{id}/reviews` response (backend `summary.ReviewsPage`):
 * `{items, total, page}`. `total` defaults to the number of `items` on the page
 * (override it to simulate more pages than the single page handed in).
 */
export function makeReviewsPage(
  items: ReviewItem[],
  overrides: Partial<ReviewsPage> = {},
): ReviewsPage {
  return { items, total: items.length, page: 1, ...overrides };
}

// ── Chat history fixtures (guardrailed-chat task 6.1) ───────────────────────

import type {
  ChatExchange,
  CitationSnippet,
  HistoryPage,
  RefreshMarker,
  TimelineItem,
} from "../api/chat";

/**
 * Build a saved Exchange timeline row in the shape the history endpoint returns
 * (backend Exchange "Data Models" shape + the history layer's `type` and
 * `is_stale`). Defaults to a fresh, in-scope Exchange on version 1; override
 * `is_stale`/`data_version` to exercise the version label, or `scope` for a
 * decline.
 */
export function makeChatExchange(overrides: Partial<ChatExchange> = {}): ChatExchange {
  return {
    type: "exchange",
    id: "ex-1",
    dataset_id: "ds-1",
    data_version: 1,
    conversation_id: "conv-1",
    asked_at: "2026-09-02T12:00:00Z",
    answered_at: "2026-09-02T12:00:03Z",
    question: "What do reviewers say about support?",
    answer: "Support is praised for speed [r_0001].",
    citations: ["r_0001"],
    dropped_citations: 0,
    citation_snippets: { r_0001: makeCitationSnippet() },
    scope: "in_scope",
    scope_category: null,
    is_stale: false,
    ...overrides,
  };
}

/** Build one saved citation snippet (design `citation_snippets` value). */
export function makeCitationSnippet(
  overrides: Partial<CitationSnippet> = {},
): CitationSnippet {
  return { text: "Support was great.", rating: 5, date: "2026-08-14", ...overrides };
}

/**
 * Build a refresh-marker timeline row (backend `history.build_markers`).
 * Defaults to a completed v2 marker; override `state`/`version` for pending or
 * failed markers.
 */
export function makeRefreshMarker(overrides: Partial<RefreshMarker> = {}): RefreshMarker {
  return {
    type: "refresh_marker",
    version: 2,
    state: "completed",
    trigger: "manual_refresh",
    requested_at: "2026-09-03T08:00:00Z",
    completed_at: "2026-09-03T08:20:00Z",
    review_count: 212,
    previous_review_count: 180,
    ...overrides,
  };
}

/**
 * Build a `GET /datasets/{id}/chat/history` page (backend
 * `history.HistoryPage.to_dict`): `{items, next_before, has_more}`, items
 * oldest-first. Defaults to a terminal page (no older history); pass
 * `next_before` + `has_more: true` to simulate an older page the pane can load.
 */
export function makeHistoryPage(
  items: TimelineItem[],
  overrides: Partial<HistoryPage> = {},
): HistoryPage {
  return { items, next_before: null, has_more: false, ...overrides };
}

// ── Chat suggestions fixtures (guardrailed-chat task 6.5) ───────────────────

/**
 * Build a `GET /datasets/{id}/chat/suggestions` response (backend
 * `app/chat/suggestions_api.py`): `{ suggestions: string[] }` with 3–4 starter
 * questions. Defaults to a theme-based set; pass general fallbacks (or any
 * array) to exercise the fallback path — the component renders either the same.
 */
export function makeSuggestions(
  suggestions: string[] = [
    "What do reviewers say about customer support?",
    "What do reviewers say about pricing?",
    "What do reviewers say about ease of use?",
  ],
): { suggestions: string[] } {
  return { suggestions };
}
