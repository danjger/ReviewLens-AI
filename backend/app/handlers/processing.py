"""Processing queue handler (review-analysis spec).

The consumer runtime (:mod:`app.consumer`) routes the ``processing`` queue to
this module and reads ``PROCESSING_QUEUE_URL``; this module supplies the
handler *business logic*. For each ``{dataset_id, version}`` message it runs
the analysis pipeline:

    start → collect pages → extract → dedupe → profile → metrics → complete

**Tasks 1.1–1.2 implement the handler skeleton, the time-budget plumbing, and
the start guard.** ``_run_pipeline`` now runs the start guard first and stops
after it; the remaining stages are added by later subtasks:

- the start guard (1.2) — ``decide_start`` / ``_start_guard`` apply
  Requirement 1.2 and transition ``requested → processing``;
- the sweeper and DLQ consumer (1.3);
- page collection, extraction, dedupe, profiling, metrics, and completion
  (tasks 2–6).

The queue, FIFO group (``dataset_id``) and deduplication (``dataset_id:version``)
IDs, dead-letter queue, redrive count (3), and the 6×-timeout visibility window
are provisioned by ``platform-foundation`` in the Api and Workers CDK stacks;
nothing here constructs them.

Engineering rules honoured:

- **Stateless.** No correctness depends on process memory or local disk; all
  shared state is the dataset row (through :mod:`app.core.db` /
  :mod:`app.db.status`) and the dataset's S3 objects (through
  :mod:`app.storage.keys`). The time budget is the one piece of per-invocation
  state and it is derived fresh from a monotonic clock on every message.
- **Same code, both compute modes.** This is a plain ``Handler`` with
  ``handle(body, meta)``; no Lambda event shapes are imported here. The runtime
  decides Lambda vs. container.
- **Idempotent.** The message body carries IDs only; every output key includes
  the version, so a redelivery (same ``dataset_id:version``) reprocesses the
  same version and overwrites its outputs. The start guard (task 1.2) makes a
  retry of an in-flight version a no-op continuation rather than a conflict.
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass
from typing import Any

from selectolax.parser import HTMLParser
from sqlalchemy import text

from app.consumer import MessageMeta
from app.core.db import session_scope
from app.db.models import DatasetStatus, SourceType
from app.db.status import transition
from app.extraction.errors import AIUnavailable
from app.extraction.models import ExtractionPlan
from app.handlers import (
    collection,
    completion,
    dedupe,
    extraction_stage,
    metrics,
    upload_extraction,
)
from app.handlers._timebudget import TIME_BUDGET_SECONDS, TimeBudget
from app.storage import keys, s3
from app.worker.ai import profile as profile_ai
from app.worker.ai import sentiment as sentiment_ai
from app.worker.ai import themes as themes_ai

logger = logging.getLogger(__name__)

# ``TimeBudget`` and ``TIME_BUDGET_SECONDS`` live in :mod:`app.handlers._timebudget`
# so the collection stage (and later stages) can share them without importing
# this handler module. They are re-exported here for backwards compatibility.
__all__ = ["TIME_BUDGET_SECONDS", "ProcessingHandler", "ProcessingMessage", "TimeBudget", "handler"]


# ---------------------------------------------------------------------------
# Message body
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessingMessage:
    """The parsed ``processing-queue`` message body.

    Bodies are small JSON objects with IDs only (repo convention); the dataset
    row and S3 objects hold everything else.
    """

    dataset_id: str
    version: int

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> ProcessingMessage:
        """Parse and validate a raw message body.

        Raises:
            ValueError: If ``dataset_id`` or ``data_version`` is missing or malformed.
                A malformed body is a programming/producer error, not a
                transient one; it is raised so the runtime routes the message to
                the DLQ rather than silently dropping work.
        """
        # The canonical version key is ``data_version`` — what every producer
        # (Add, Refresh, sweeper) enqueues and what the DLQ handler reads; the
        # legacy ``version`` key is accepted as a synonym for forward/backward
        # compatibility (mirrors ``app.handlers.dlq._parse``). Reading the wrong
        # key here previously made every real message fail (producers send
        # ``data_version``), so the version is sourced from both.
        if "dataset_id" not in body:
            raise ValueError(f"processing message missing 'dataset_id': {body!r}")
        dataset_id = body["dataset_id"]
        if "data_version" in body:
            version = body["data_version"]
        elif "version" in body:
            version = body["version"]
        else:
            raise ValueError(f"processing message missing 'data_version': {body!r}")

        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError(f"processing message has invalid dataset_id: {body!r}")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError(f"processing message has non-integer version: {body!r}")
        if version < 1:
            raise ValueError(f"processing message has non-positive version: {body!r}")

        return cls(dataset_id=dataset_id, version=version)


# ---------------------------------------------------------------------------
# Start guard
# ---------------------------------------------------------------------------


class GuardDecision(enum.Enum):
    """What the start guard decided for one processing message.

    The three outcomes map directly onto Requirement 1.2 and the design's
    opening ``flowchart`` node:

    - :data:`START` – the version is ``requested``; the pipeline must transition
      the dataset to ``processing`` and append a ``processing`` event, then
      continue (Requirement 1.1).
    - :data:`CONTINUE` – the message is a retry of the version already
      ``processing``; continue the pipeline **without** a redundant transition
      (the no-op branch; design: "The start guard treats ``processing`` for the
      same version as a retry and continues").
    - :data:`SKIP` – the message is superseded (older than ``data_version``),
      that version already completed, or the dataset is archived; skip it
      without error (Requirement 1.2).
    """

    START = "start"
    CONTINUE = "continue"
    SKIP = "skip"


@dataclass(frozen=True)
class DatasetState:
    """The dataset facts the start guard decides on.

    Read once, under a row lock, from the dataset row and its
    ``dataset_versions`` row for the message's version. Keeping the decision a
    pure function of this value object (see :func:`decide_start`) makes every
    branch and Property 6 testable without a database.

    Attributes:
        status: The dataset's current lifecycle status.
        data_version: The latest version attempted (``datasets.data_version``).
        version_completed: Whether this message's version already finished
            (its ``dataset_versions`` row has an ``outcome``).
        archived: Whether the dataset is archived (``archived_at`` is set).
    """

    status: DatasetStatus
    data_version: int
    version_completed: bool
    archived: bool


def decide_start(message_version: int, state: DatasetState) -> GuardDecision:
    """Decide whether to start, continue, or skip, from pure inputs.

    This is the whole of Requirement 1.2 as a pure function so each branch has a
    unit test and Property 6 can sweep the input space. Skip conditions are
    checked first because they hold regardless of status: a superseded,
    already-completed, or archived version is never processed, even if the row
    still reads ``requested`` or ``processing`` (for example a stale retry that
    was superseded by a newer refresh).

    Args:
        message_version: The version carried by the processing message.
        state: The current dataset facts (see :class:`DatasetState`).

    Returns:
        :data:`GuardDecision.SKIP` when the message's version is older than the
        dataset's ``data_version``, that version already completed, or the
        dataset is archived; :data:`GuardDecision.START` when the dataset is
        ``requested``; :data:`GuardDecision.CONTINUE` when the message is a
        retry of the version already ``processing``; otherwise
        :data:`GuardDecision.SKIP` (e.g. the dataset already finished
        ``updated``/``failed`` for this version, or the status does not match
        this version).
    """
    # Skip, no error (Requirement 1.2): superseded by a newer version, this
    # version already finished, or the dataset was archived.
    if message_version < state.data_version:
        return GuardDecision.SKIP
    if state.version_completed:
        return GuardDecision.SKIP
    if state.archived:
        return GuardDecision.SKIP

    # A message newer than the row's data_version has no row to act on yet; the
    # version that produced it owns the increment. Treat as skip (no error).
    if message_version > state.data_version:
        return GuardDecision.SKIP

    # version == data_version and the dataset is live.
    if state.status is DatasetStatus.REQUESTED:
        # Requirement 1.1: begin processing this version.
        return GuardDecision.START
    if state.status is DatasetStatus.PROCESSING:
        # Requirement 1.2: a retry of the version already in flight continues
        # without a redundant transition (design no-op branch).
        return GuardDecision.CONTINUE

    # updated / failed for the current version with no completion row recorded:
    # nothing to do. Defensive; completed versions are caught above.
    return GuardDecision.SKIP


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class ProcessingHandler:
    """Queue handler that runs the analysis pipeline for one dataset version.

    This skeleton (task 1.1) parses the message, starts the time budget, and
    hands off to :meth:`_run_pipeline`. Later subtasks implement the stages
    inside ``_run_pipeline`` and the final-attempt failure handling.
    """

    queue: str = "processing"

    def handle(self, body: dict[str, Any], meta: MessageMeta) -> None:
        message = ProcessingMessage.from_body(body)
        budget = TimeBudget.start()

        logger.info(
            "processing %s v%d (attempt %d/3, %.0fs budget)",
            message.dataset_id,
            message.version,
            meta.receive_count,
            budget.budget_seconds,
        )
        self._run_pipeline(message, meta, budget)

    def _run_pipeline(
        self,
        message: ProcessingMessage,
        meta: MessageMeta,
        budget: TimeBudget,
    ) -> None:
        """Run the pipeline stages in order (filled in by later subtasks).

        The start guard (task 1.2) runs first and decides whether to begin,
        continue a retry, or skip this message (Requirement 1.2). When the guard
        skips, the pipeline returns immediately with no side effects. The
        analysis stages (page collection, extraction, dedupe, profiling,
        metrics) then run and the completion stage (task 6) ends the version.

        Final-attempt failure handling (Requirements 7.2, 7.4): the analysis
        stages run inside a ``try``. On a *non-final* attempt any exception
        (including :class:`~app.extraction.errors.AIUnavailable`) propagates so
        SQS redelivers the message with backoff (Requirement 7.1). On the *final*
        attempt the error is caught and the version is moved to ``failed`` with a
        user-safe message — the AI-unavailable message for an AI outage, a
        generic message otherwise — through the shared completion stage so the
        ``dataset_versions`` row is completed and the event published exactly
        once (design "Error Handling").
        """
        guard = self._start_guard(message)
        if guard is GuardDecision.SKIP:
            logger.info(
                "skipping %s v%d: superseded, completed, or archived",
                message.dataset_id,
                message.version,
            )
            return

        try:
            self._run_stages(message, budget)
        except Exception as error:  # noqa: BLE001 - re-raised below unless final
            if not meta.is_final_attempt:
                # Non-final attempt: let SQS retry with backoff (Req 7.1/7.4).
                logger.warning(
                    "processing %s v%d failed on attempt %d/3; will retry: %s",
                    message.dataset_id,
                    message.version,
                    meta.receive_count,
                    error,
                )
                raise
            # Final attempt: fail the version with a user-safe message.
            logger.warning(
                "processing %s v%d failed on final attempt; marking failed: %s",
                message.dataset_id,
                message.version,
                error,
            )
            self._fail_final(message, budget, error)

    def _run_stages(self, message: ProcessingMessage, budget: TimeBudget) -> None:
        """Run the analysis stages and complete the version (success paths).

        Separated from :meth:`_run_pipeline` so the final-attempt ``except`` in
        the caller wraps exactly the fallible stages. On a successful run this
        ends the version through the completion stage (task 6): ``updated`` with
        at least one review, or ``failed`` for zero reviews (a normal completion,
        not an exception).
        """
        # Stage: collect pages (task 2). URL datasets walk the review listing
        # from page 1; upload datasets skip collection (Requirement 2.5). The
        # collection stage returns the plan it loaded (``None`` for uploads) so
        # the extraction stage reuses it rather than re-reading S3.
        collected, plan = self._collect_pages(message, budget)

        # Stage: extract. URL datasets read every captured page with the saved
        # plan (task 3.1). Upload datasets (collection skipped them) build their
        # Normalized Reviews from the saved column mapping and the uploaded CSV,
        # skipping empty rows and applying the MAX_REVIEWS keep rule with a
        # warning (task 3.2, Requirement 3.4). The upload branch is chosen by the
        # collection stage's explicit skip reason, not by inferring it from an
        # empty page list, so a URL dataset that happened to capture only page 1
        # is never mistaken for an upload.
        is_upload = collected.stop_reason == collection.STOP_SKIPPED_UPLOAD
        # Warnings accumulate across stages into ``metrics.warnings`` (collection
        # page-failure / script-only / time-budget notices, the upload keep-rule
        # notice, and the themes-omitted notice from the AI stage below).
        warnings: list[str] = list(collected.warnings)
        page_details: list[extraction_stage.PageDetail] = []
        # ``skipped`` is the number of rows/reviews skipped (Requirement 5.1):
        # for uploads, empty rows plus rows left out by the MAX_REVIEWS keep rule.
        skipped = 0

        if is_upload:
            upload = self._extract_upload(message)
            extracted_reviews = upload.reviews
            warnings.extend(upload.warnings)
            skipped = upload.skipped_empty + upload.left_out
            logger.debug(
                "upload extraction for %s v%d: %d review(s), %d skipped empty, %d left out",
                message.dataset_id,
                message.version,
                len(extracted_reviews),
                upload.skipped_empty,
                upload.left_out,
            )
        else:
            extracted = self._extract_pages(collected, plan)
            extracted_reviews = extracted.reviews
            page_details = extracted.pages
            logger.debug(
                "collected %d page(s), extracted %d review(s) for %s v%d (stop=%s)",
                len(collected.pages),
                len(extracted_reviews),
                message.dataset_id,
                message.version,
                collected.stop_reason,
            )

        # Stage: dedupe across pages (task 3.3). Both the URL and upload branches
        # feed one dedupe (design flowchart node D): remove duplicate reviews by
        # normalized text + author + date, keeping the earliest page's copy
        # (Requirement 3.3).
        reviews = dedupe.dedupe(extracted_reviews)
        logger.debug(
            "deduped %d review(s) to %d for %s v%d",
            len(extracted_reviews),
            len(reviews),
            message.dataset_id,
            message.version,
        )

        # Stage: entity profile (task 4.1). Derived only from the captured
        # content: the page title/header (URL datasets) or the upload name, the
        # plan's entity hint, and the first reviews. Content-only is enforced by
        # the prompt (Requirement 4.1).
        profile = self._build_profile(
            message, plan=plan, reviews=reviews, collected=collected, is_upload=is_upload
        )

        # Stage: sentiment + themes (tasks 4.2, 4.3). Both are AI tasks over the
        # deduped reviews; the sentiment labels are position-aligned with
        # ``reviews`` and the themes carry corpus (index-string) example ids that
        # the metrics stage maps onto the minted review ids.
        sentiments = sentiment_ai.classify_sentiment(reviews)
        themes, theme_warnings = themes_ai.extract_themes(reviews)
        warnings.extend(theme_warnings)

        # Stage: metrics + output (task 5). Compute the metrics dict and write
        # ``reviews/v{n}.json`` idempotently by version (Requirements 5.1-5.6,
        # 3.5, 7.3). The ``metrics`` dict is returned for task 6 to write to the
        # row (only on a successful ``updated`` version); this stage does not
        # transition the status or touch the dataset row.
        duration_ms = int(budget.elapsed() * 1000)
        result = metrics.run(
            dataset_id=message.dataset_id,
            version=message.version,
            reviews=reviews,
            sentiments=sentiments,
            themes=themes,
            profile=profile,
            pages=page_details,
            skipped=skipped,
            warnings=warnings,
            duration_ms=duration_ms,
        )

        # Stage: complete (task 6). Zero reviews is a normal completion ending in
        # `failed` (Requirement 6.2); at least one review ends in `updated` with
        # active_version advanced and the metrics column written (Requirement
        # 6.1). The actual result is recorded next to the viability prediction
        # (Requirement 6.4) on either path.
        self._complete(
            message,
            metrics_dict=result.metrics,
            review_count=result.review_count,
            page_details=page_details,
            duration_ms=duration_ms,
            error=None,
        )

    def _complete(
        self,
        message: ProcessingMessage,
        *,
        metrics_dict: dict[str, Any],
        review_count: int,
        page_details: list[extraction_stage.PageDetail],
        duration_ms: int,
        error: BaseException | None,
    ) -> None:
        """End the version through the completion stage (review-analysis task 6).

        Builds the :class:`~app.handlers.completion.CompletionDecision` from the
        run's facts and delegates the database side to
        :func:`app.handlers.completion.complete`. ``error`` is ``None`` on the
        normal success/zero-reviews path and set only on the final-attempt
        failure path (:meth:`_fail_final`).
        """
        extraction = metrics_dict.get("extraction", {}) if metrics_dict else {}
        method = str(extraction.get("method", "")) or "unknown"
        fallbacks = sum(1 for page in page_details if page.fallback)
        pages_captured = (
            int(metrics_dict.get("pages_captured", len(page_details)))
            if metrics_dict
            else len(page_details)
        )

        decision = completion.decide_completion(
            review_count=review_count,
            error=error,
            is_refresh=message.version >= 2,
            ai_unavailable=isinstance(error, AIUnavailable),
        )
        actual = completion.ActualResult(
            reviews=review_count,
            pages=pages_captured,
            method=method,
            fallbacks=fallbacks,
        )
        completion.complete(
            dataset_id=message.dataset_id,
            version=message.version,
            decision=decision,
            metrics=metrics_dict,
            actual=actual,
            review_count=review_count,
            duration_ms=duration_ms,
        )

    def _fail_final(
        self,
        message: ProcessingMessage,
        budget: TimeBudget,
        error: BaseException,
    ) -> None:
        """Fail a version on the final attempt with a user-safe message (7.2/7.4).

        An upstream stage raised and this is the last delivery before the DLQ.
        There are no metrics or reviews for this version, so the completion
        records zero reviews, no pages, and ``active_version`` / ``metrics`` are
        left unchanged (so a failed refresh keeps its last good version —
        Requirement 6.5). The user-safe message is chosen by
        :func:`app.handlers.completion.decide_completion`: the AI-unavailable
        message for :class:`~app.extraction.errors.AIUnavailable`, otherwise a
        generic message.
        """
        self._complete(
            message,
            metrics_dict={},
            review_count=0,
            page_details=[],
            duration_ms=int(budget.elapsed() * 1000),
            error=error,
        )

    def _collect_pages(
        self,
        message: ProcessingMessage,
        budget: TimeBudget,
    ) -> tuple[collection.CollectionResult, ExtractionPlan | None]:
        """Run the page-collection stage for *message* (review-analysis task 2).

        Reads the dataset's source type and (for URL datasets) the saved
        Extraction Plan and Final URL, then delegates to
        :func:`app.handlers.collection.collect_pages`. Upload datasets return an
        empty, skipped result (Requirement 2.5) and a ``None`` plan without
        reading one. Returns both the collection result and the loaded plan so
        the extraction stage (task 3.1) reuses the plan rather than re-reading
        it from S3.
        """
        source_type, final_url = self._load_collection_inputs(message)
        if source_type is SourceType.UPLOAD:
            result = collection.collect_pages(
                message.dataset_id,
                message.version,
                plan=None,
                first_page_url="",
                budget=budget,
                is_upload=True,
            )
            return result, None

        plan = self._load_plan(message)
        result = collection.collect_pages(
            message.dataset_id,
            message.version,
            plan=plan,
            first_page_url=final_url or "",
            budget=budget,
            is_upload=False,
        )
        return result, plan

    def _extract_pages(
        self,
        collected: collection.CollectionResult,
        plan: ExtractionPlan | None,
    ) -> extraction_stage.ExtractionResult:
        """Run the per-page extraction stage (review-analysis task 3.1).

        Delegates to :func:`app.handlers.extraction_stage.extract_pages`,
        reading every collected page with the saved plan and collecting per-page
        details plus the combined reviews (Requirements 3.1, 3.2). Returns an
        empty result when there is nothing to extract: an upload dataset (no
        plan, no captured pages) builds its reviews from the mapping in task 3.2,
        and a URL dataset with no captured pages (should not happen — page 1 is
        always present) has nothing to read.
        """
        if plan is None or not collected.pages:
            return extraction_stage.ExtractionResult()
        return extraction_stage.extract_pages(collected.pages, plan)

    def _extract_upload(
        self,
        message: ProcessingMessage,
    ) -> upload_extraction.UploadExtractionResult:
        """Build Normalized Reviews for an upload dataset (review-analysis task 3.2).

        Delegates to :func:`app.handlers.upload_extraction.extract_upload`, which
        reads the saved column mapping and the uploaded CSV from the dataset's
        own S3 objects, skips rows with empty text (counting them), and keeps at
        most ``MAX_REVIEWS`` rows using the keep rule shown in the upload preview,
        recording a warning with the number of rows left out (Requirement 3.4).
        The reviews are tagged with
        :data:`app.handlers.upload_extraction.UPLOAD_SOURCE_PAGE` so the dedupe
        and output stages (tasks 3.3, 5) consume them through the same path as
        per-page reviews.
        """
        return upload_extraction.extract_upload(message.dataset_id, message.version)

    def _build_profile(
        self,
        message: ProcessingMessage,
        *,
        plan: ExtractionPlan | None,
        reviews: list[extraction_stage.CollectedReview],
        collected: collection.CollectionResult,
        is_upload: bool,
    ) -> profile_ai.EntityProfile:
        """Build the entity profile for this version (review-analysis task 4.1).

        Supplies the content-only inputs the profiler needs: for a URL dataset,
        the first captured page's ``<title>`` and main header text and the plan's
        ``entity_hint``; for an upload, the dataset name as the fallback label.
        The first reviews are passed as context. The AI call is delegated to
        :func:`app.worker.ai.profile.entity_profile`, which applies the
        low-confidence fallback (Requirement 4.2) and re-raises ``AIUnavailable``.
        """
        entity_hint = plan.entity_hint if plan is not None else None
        name, page_title = self._load_dataset_labels(message)

        if is_upload:
            return profile_ai.entity_profile(
                title="",
                header="",
                entity_hint=entity_hint,
                reviews=reviews,
                upload_name=name,
            )

        # Prefer the page title captured by the Check (``page_title`` column);
        # fall back to the dataset name. The main header is read from the first
        # captured page for extra context.
        title = page_title or name or ""
        header = self._page_header(collected)
        return profile_ai.entity_profile(
            title=title,
            header=header,
            entity_hint=entity_hint,
            reviews=reviews,
        )

    @staticmethod
    def _page_header(collected: collection.CollectionResult) -> str:
        """Read the first captured page's main header (``<h1>``) text.

        Page content read by code with selectolax (the profiler uses it only to
        identify the entity; it never echoes it back as review text). Returns
        ``""`` when there is no first page or no ``<h1>``.
        """
        if not collected.pages:
            return ""
        header_node = HTMLParser(collected.pages[0].html).css_first("h1")
        return " ".join(header_node.text().split()) if header_node is not None else ""

    def _load_dataset_labels(self, message: ProcessingMessage) -> tuple[str | None, str | None]:
        """Read the dataset's ``name`` and ``page_title`` for the profile inputs.

        ``name`` is the upload fallback label (Requirement 4.2); ``page_title``
        is the Check's captured page title used as the profiler's ``title`` for
        URL datasets. Database access goes through
        :func:`app.core.db.session_scope` only. Empty columns return ``None`` so
        the profiler falls back through its own rules.
        """
        with session_scope() as session:
            row = session.execute(
                text(
                    "SELECT name, page_title FROM datasets WHERE id = CAST(:id AS uuid)"
                ).bindparams(id=message.dataset_id),
            ).first()
        if row is None:
            return None, None
        name = (str(row[0]).strip() or None) if row[0] is not None else None
        page_title = (str(row[1]).strip() or None) if row[1] is not None else None
        return name, page_title

    def _load_collection_inputs(self, message: ProcessingMessage) -> tuple[SourceType, str | None]:
        """Read the dataset's ``source_type`` and ``final_url`` for collection.

        Database access goes through :func:`app.core.db.session_scope` only
        (no-VPC rule). The Final URL is the page-1 URL the Check landed on;
        next-page candidates are resolved against it.

        Raises:
            ValueError: If the dataset does not exist.
        """
        with session_scope() as session:
            row = session.execute(
                text(
                    "SELECT source_type, final_url FROM datasets WHERE id = CAST(:id AS uuid)"
                ).bindparams(id=message.dataset_id),
            ).first()
        if row is None:
            raise ValueError(f"Dataset {message.dataset_id!r} does not exist")
        return SourceType(str(row[0])), (str(row[1]) if row[1] is not None else None)

    def _load_plan(self, message: ProcessingMessage) -> ExtractionPlan:
        """Read and validate the saved Extraction Plan for this version.

        The plan was written to ``raw/v{n}/plan.json`` by Add/Refresh
        (dataset-ingestion). Its key comes from :mod:`app.storage.keys`.
        """
        plan_key = keys.dataset_raw_plan(message.dataset_id, message.version)
        return ExtractionPlan.model_validate(json.loads(s3.get_text(plan_key)))

    def _start_guard(self, message: ProcessingMessage) -> GuardDecision:
        """Apply the start guard for one message (review-analysis task 1.2).

        Reads the dataset's current state under a row lock, decides with the
        pure :func:`decide_start`, and — only when the decision is
        :data:`GuardDecision.START` — transitions the dataset to ``processing``
        and appends a ``processing`` event through :func:`app.db.status.transition`
        (Requirements 1.1 and 8.1: the transition publishes
        ``dataset.status.changed``). ``CONTINUE`` and ``SKIP`` make no status
        change, so a retry of an in-flight version is a no-op continuation
        (idempotent) rather than a redundant transition.

        Returns:
            The :class:`GuardDecision` so the caller can stop (``SKIP``) or
            proceed (``START`` / ``CONTINUE``).
        """
        state = self._load_state(message)
        decision = decide_start(message.version, state)

        if decision is GuardDecision.START:
            # Requirement 1.1 + 8.1: move requested → processing and append a
            # `processing` event in one transaction; the transition publishes
            # a dataset.status.changed event. The version is recorded on the
            # event so consumers can tell which version started.
            transition(
                message.dataset_id,
                DatasetStatus.PROCESSING,
                "processing",
                extra={"version": message.version},
            )

        return decision

    def _load_state(self, message: ProcessingMessage) -> DatasetState:
        """Read the guard's inputs for *message* under a row lock.

        One statement reads the dataset's ``status``, ``data_version``, and
        archived flag with ``SELECT ... FOR UPDATE`` so the state cannot change
        between the read and the guard's transition; a second reads whether this
        version already has a terminal ``outcome`` in ``dataset_versions``. Only
        the dataset row is locked — the version-completion check is a plain read
        of an append-once column.

        Database access goes through :func:`app.core.db.session_scope` only (no
        VPC / Data-API rule).

        Raises:
            ValueError: If the dataset does not exist (a malformed/producer
                error, surfaced so the runtime routes it to the DLQ).
        """
        with session_scope() as session:
            row = session.execute(
                text(
                    "SELECT status, data_version, (archived_at IS NOT NULL) AS archived "
                    "FROM datasets WHERE id = CAST(:id AS uuid) FOR UPDATE"
                ).bindparams(id=message.dataset_id),
            ).first()
            if row is None:
                raise ValueError(f"Dataset {message.dataset_id!r} does not exist")

            version_row = session.execute(
                text(
                    "SELECT outcome FROM dataset_versions "
                    "WHERE dataset_id = CAST(:id AS uuid) AND version = :version"
                ).bindparams(id=message.dataset_id, version=message.version),
            ).first()

        version_completed = version_row is not None and version_row[0] is not None
        return DatasetState(
            status=DatasetStatus(str(row[0])),
            data_version=int(row[1]),
            version_completed=version_completed,
            archived=bool(row[2]),
        )


handler = ProcessingHandler()
