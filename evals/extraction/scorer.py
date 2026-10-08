"""Scoring for the extraction evaluation suite (review-extraction, Requirement 8.2).

This module runs the extraction methods against each labelled page and scores
them. It is deliberately split from :mod:`run` (the CLI / report writer) so the
scoring logic is importable and unit-testable without a live model.

Methods scored (Requirement 8.2):

- ``structured`` — :func:`app.extraction.parse_structured` (fully OFFLINE, no AI).
- ``selectors``  — :func:`app.extraction.extract.extract_by_selectors` applied with
  the page's labelled known-good selector set (fully OFFLINE, no AI). A page with
  no labelled selectors reports this method as not-applicable.
- ``ai_direct``  — the Locator path (``clean → locate → postprocess``). Requires
  the AI client, so it is scored only when ``include_ai=True`` (the live run,
  Task 9.4). Offline it is reported as not-applicable.
- ``auto``       — the automatic choice: :func:`app.extraction.build_plan` to pick
  the method, then :func:`app.extraction.extract_page` to extract. Also AI-backed,
  so scored only when ``include_ai=True``.

Each method's reviews are matched to the page's expected reviews by normalized
text: an expected review matches an extracted review when the expected ``prefix``
(normalized) is contained in the extracted text (normalized). From the matching
we compute precision, recall, per-field accuracy (rating / date / author),
next-page detection correctness, AI tokens used, and elapsed time (Requirement
8.2). A per-method :class:`MethodScore` and a per-page :class:`PageScore` roll up
into a :class:`Report` with totals.

The AI-backed methods go through the single instrumented client
(:class:`app.core.ai.AiClient`); :func:`score_pages` optionally wraps it to count
tokens so the report's "AI tokens" column is populated during a live run. Nothing
here hard-depends on an API key: ``include_ai=False`` keeps the whole run offline
and deterministic, which is what Task 9.1 and the offline tests exercise.

Thresholds (Requirement 8.3) are *computed and reported* here via
:func:`auto_meets_thresholds`; the failing CI gate that uses them is Task 9.2.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.extraction import build_plan, extract_page, parse_structured
from app.extraction.extract import extract_by_selectors
from app.extraction.models import ExtractionPlan, LocatorSelectors, VerifiedReview
from app.extraction.pagination import next_page as resolve_next_page

from evals.extraction.labels import ExpectedReview, LabeledPage, SelectorSet

# Threshold constants (Requirement 8.3), applied to the automatic choice on
# pages labelled ``will_work``.
AUTO_PRECISION_THRESHOLD = 0.98
AUTO_RECALL_THRESHOLD = 0.90

# Verdict-accuracy threshold (dataset-ingestion Requirement 3.14): the viability
# verdict produced by ``ingestion.viability.assess()`` is scored against the
# labelled ``verdict`` field of every page, and the evaluation fails below 0.90.
VERDICT_ACCURACY_THRESHOLD = 0.90

# Method identifiers reported in the output.
METHOD_STRUCTURED = "structured"
METHOD_SELECTORS = "selectors"
METHOD_AI_DIRECT = "ai_direct"
METHOD_AUTO = "auto"

#: The methods scored on every run, in report order. ``auto`` is listed last so
#: the report reads method-by-method then the automatic choice.
ALL_METHODS: tuple[str, ...] = (
    METHOD_STRUCTURED,
    METHOD_SELECTORS,
    METHOD_AI_DIRECT,
    METHOD_AUTO,
)

_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Normalize text for matching: NFKC, collapse whitespace, strip, lowercase."""
    normalized = unicodedata.normalize("NFKC", text)
    return _WS_RE.sub(" ", normalized).strip().lower()


# ---------------------------------------------------------------------------
# Score containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldAccuracy:
    """Accuracy of one extracted field over the matched, labelled reviews.

    ``correct`` / ``total`` where ``total`` is the number of matched reviews that
    carry a labelled value for the field, and ``correct`` is how many of those
    had an equal extracted value. ``value`` is ``correct / total`` (or ``None``
    when there is nothing to score, so an unlabelled field does not drag the
    score down).
    """

    correct: int = 0
    total: int = 0

    @property
    def value(self) -> float | None:
        return (self.correct / self.total) if self.total else None


@dataclass
class MethodScore:
    """Scores for one method on one page (Requirement 8.2)."""

    method: str
    applicable: bool = True
    note: str = ""
    expected: int = 0
    extracted: int = 0
    matched_expected: int = 0
    matched_extracted: int = 0
    rating: FieldAccuracy = field(default_factory=FieldAccuracy)
    date: FieldAccuracy = field(default_factory=FieldAccuracy)
    author: FieldAccuracy = field(default_factory=FieldAccuracy)
    next_page_correct: bool | None = None
    tokens: int = 0
    seconds: float = 0.0

    @property
    def precision(self) -> float | None:
        """matched extracted / total extracted (``None`` when nothing extracted)."""
        return (self.matched_extracted / self.extracted) if self.extracted else None

    @property
    def recall(self) -> float | None:
        """matched expected / total expected (``None`` when nothing expected)."""
        return (self.matched_expected / self.expected) if self.expected else None


@dataclass
class PageScore:
    """All method scores for a single page."""

    page: str
    verdict: str
    tags: tuple[str, ...]
    methods: dict[str, MethodScore] = field(default_factory=dict)


@dataclass(frozen=True)
class VerdictPageScore:
    """The viability-verdict outcome for one page (dataset-ingestion R3.14).

    ``assess()`` is run on the page and its ``.verdict`` label compared to the
    page's hand-checked label. ``scored`` is ``False`` when the verdict was not
    evaluated for this page on this run — this happens offline for pages whose
    assessment would need an AI call (anything not short-circuited by the
    rule-based pre-scan), mirroring how the AI-backed methods are skipped
    offline. ``predicted`` is ``None`` when the page was not scored.
    """

    page: str
    expected: str
    predicted: str | None = None
    scored: bool = False
    note: str = ""

    @property
    def correct(self) -> bool:
        """Whether the predicted verdict matched the labelled one (scored only)."""
        return self.scored and self.predicted == self.expected


@dataclass(frozen=True)
class VerdictScore:
    """Aggregate verdict accuracy across the scored pages (R3.14)."""

    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float | None:
        """``correct / total`` over scored pages, or ``None`` when none scored."""
        return (self.correct / self.total) if self.total else None


@dataclass
class Report:
    """The full evaluation report: per-page scores plus whether AI ran."""

    pages: list[PageScore] = field(default_factory=list)
    include_ai: bool = False
    verdicts: list[VerdictPageScore] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Matching and field scoring
# ---------------------------------------------------------------------------


def _match(
    expected: tuple[ExpectedReview, ...],
    extracted: list[VerifiedReview],
) -> list[tuple[ExpectedReview, VerifiedReview]]:
    """Match expected reviews to extracted reviews by normalized prefix containment.

    Each expected review matches the first not-yet-used extracted review whose
    normalized text contains the expected (normalized) prefix. Greedy and
    deterministic (expected reviews are consumed in order), which is sufficient
    because the seed's prefixes are unambiguous.
    """
    pairs: list[tuple[ExpectedReview, VerifiedReview]] = []
    used: set[int] = set()
    norm_extracted = [(_normalize(r.text), r) for r in extracted]
    for exp in expected:
        prefix = _normalize(exp.prefix)
        for index, (norm, review) in enumerate(norm_extracted):
            if index in used:
                continue
            if prefix and prefix in norm:
                used.add(index)
                pairs.append((exp, review))
                break
    return pairs


def _date_equal(expected: str, actual: str | None) -> bool:
    """Compare dates leniently: equal, or the expected ISO date is a substring."""
    if actual is None:
        return False
    actual_norm = actual.strip()
    return actual_norm == expected or expected in actual_norm


def _author_equal(expected: str, actual: str | None) -> bool:
    """Compare authors by normalized containment (either direction)."""
    if actual is None:
        return False
    e = _normalize(expected)
    a = _normalize(actual)
    return bool(e) and (e in a or a in e)


def _rating_equal(expected: float, actual: float | None) -> bool:
    """Compare ratings numerically with a small tolerance."""
    if actual is None:
        return False
    return abs(expected - actual) < 1e-6


def _score_fields(
    pairs: list[tuple[ExpectedReview, VerifiedReview]],
) -> tuple[FieldAccuracy, FieldAccuracy, FieldAccuracy]:
    """Compute rating/date/author accuracy over the matched pairs."""
    rating_total = rating_ok = 0
    date_total = date_ok = 0
    author_total = author_ok = 0
    for exp, got in pairs:
        if exp.rating is not None:
            rating_total += 1
            rating_ok += int(_rating_equal(exp.rating, got.rating))
        if exp.date is not None:
            date_total += 1
            date_ok += int(_date_equal(exp.date, got.date))
        if exp.author is not None:
            author_total += 1
            author_ok += int(_author_equal(exp.author, got.author))
    return (
        FieldAccuracy(rating_ok, rating_total),
        FieldAccuracy(date_ok, date_total),
        FieldAccuracy(author_ok, author_total),
    )


def _score_method(
    method: str,
    page: LabeledPage,
    extracted: list[VerifiedReview],
    *,
    next_url: str | None,
    tokens: int,
    seconds: float,
) -> MethodScore:
    """Roll a method's extracted reviews + next page into a :class:`MethodScore`."""
    pairs = _match(page.reviews, extracted)
    matched_expected = len(pairs)
    matched_extracted = len({id(got) for _, got in pairs})
    rating, date, author = _score_fields(pairs)
    return MethodScore(
        method=method,
        applicable=True,
        expected=len(page.reviews),
        extracted=len(extracted),
        matched_expected=matched_expected,
        matched_extracted=matched_extracted,
        rating=rating,
        date=date,
        author=author,
        next_page_correct=(next_url == page.next_page),
        tokens=tokens,
        seconds=seconds,
    )


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


class _TokenCounter:
    """Accumulates AI token usage observed during a scored method run.

    Wraps the process-wide :class:`app.core.ai.AiClient` by patching its
    ``create_message`` to add up ``input_tokens + output_tokens`` from each SDK
    response. Offline methods make no AI call, so their count stays zero.
    """

    def __init__(self) -> None:
        self.tokens = 0

    @contextmanager
    def tracking(self) -> Iterator[None]:
        """Patch the live client's ``create_message`` to tally tokens for a block."""
        from app.core import ai as ai_module

        client = ai_module.get_ai_client()
        original = client.create_message

        def wrapped(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - passthrough
            response = original(*args, **kwargs)
            usage = getattr(response, "usage", None)
            self.tokens += int(getattr(usage, "input_tokens", 0) or 0)
            self.tokens += int(getattr(usage, "output_tokens", 0) or 0)
            return response

        client.create_message = wrapped  # type: ignore[method-assign]
        try:
            yield
        finally:
            client.create_message = original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Per-method runners
# ---------------------------------------------------------------------------


def _timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    """Run ``fn`` and return ``(result, elapsed_seconds)``."""
    start = time.monotonic()
    result = fn()
    return result, time.monotonic() - start


def _run_structured(page: LabeledPage) -> MethodScore:
    """Score the ``structured`` method (offline): parse JSON-LD / microdata."""
    (reviews_result, _), seconds = _timed(
        lambda: (parse_structured(page.html, _visible_text(page.html)), None)
    )
    reviews = list(reviews_result.reviews)
    # Structured data carries no next-page link; expected is matched against None.
    return _score_method(
        METHOD_STRUCTURED, page, reviews, next_url=None, tokens=0, seconds=seconds
    )


def _run_selectors(page: LabeledPage) -> MethodScore:
    """Score the ``selectors`` method (offline) using the labelled selector set.

    A page without a labelled selector set reports not-applicable (there is no
    AI-built plan offline to supply selectors).
    """
    if page.selectors is None:
        return MethodScore(
            method=METHOD_SELECTORS,
            applicable=False,
            note="no labelled selectors",
            expected=len(page.reviews),
        )
    plan = _plan_from_selectors(page.selectors)

    def _extract() -> list[VerifiedReview]:
        reviews, _discarded = extract_by_selectors(page.html, plan)
        return reviews

    reviews, seconds = _timed(_extract)
    next_result = resolve_next_page(page.html, page.url, plan, None)
    return _score_method(
        METHOD_SELECTORS, page, reviews, next_url=next_result.url, tokens=0, seconds=seconds
    )


def _run_ai_direct(page: LabeledPage, counter: _TokenCounter) -> MethodScore:
    """Score the ``ai_direct`` method (live): the Locator path via ``extract_page``."""
    plan = _ai_direct_plan()

    def _extract() -> Any:  # noqa: ANN401 - PageResult
        with counter.tracking():
            return extract_page(page.html, page.url, plan, is_last=True)

    result, seconds = _timed(_extract)
    return _score_method(
        METHOD_AI_DIRECT,
        page,
        list(result.reviews),
        next_url=result.next_page.url,
        tokens=counter.tokens,
        seconds=seconds,
    )


def _run_auto(page: LabeledPage, counter: _TokenCounter) -> MethodScore:
    """Score the automatic choice (live): ``build_plan`` then ``extract_page``."""

    def _extract() -> Any:  # noqa: ANN401 - PageResult
        with counter.tracking():
            plan, _first = build_plan(page.html, page.url, "")
            return extract_page(page.html, page.url, plan, is_last=True)

    result, seconds = _timed(_extract)
    return _score_method(
        METHOD_AUTO,
        page,
        list(result.reviews),
        next_url=result.next_page.url,
        tokens=counter.tokens,
        seconds=seconds,
    )


# ---------------------------------------------------------------------------
# Plan helpers (offline selectors path and the ai_direct dispatch)
# ---------------------------------------------------------------------------


def _plan_from_selectors(selectors: SelectorSet) -> ExtractionPlan:
    """Build a minimal ``selectors`` plan from a labelled selector set.

    ``per_page_rate=0`` disables the low-yield fallback (there is nothing to fall
    back to offline), and ``extract_by_selectors`` reads reviews with code only.
    """
    from datetime import UTC, datetime

    return ExtractionPlan(
        created_at=datetime.now(UTC).isoformat(),
        method="selectors",
        selectors=LocatorSelectors(
            item=selectors.item,
            text=selectors.text,
            rating=selectors.rating,
            date=selectors.date,
            author=selectors.author,
            title=selectors.title,
        ),
        rating_scale=5,
        next_page_rule=_none_rule(),
    )


def _ai_direct_plan() -> ExtractionPlan:
    """A plan whose method is ``ai_direct`` so ``extract_page`` runs the Locator."""
    from datetime import UTC, datetime

    return ExtractionPlan(
        created_at=datetime.now(UTC).isoformat(),
        method="ai_direct",
        next_page_rule=_none_rule(),
    )


def _none_rule() -> Any:  # noqa: ANN401 - avoids importing NextPageRule at module top
    from app.extraction.models import NextPageRule

    return NextPageRule(type="none")


def _visible_text(html: str) -> str:
    """Return the page's visible body text (scripts stripped) for structured verify."""
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    for tag in ("script", "style", "noscript", "template"):
        for node in tree.css(tag):
            node.decompose()
    body = tree.body
    return body.text() if body is not None else ""


# ---------------------------------------------------------------------------
# Verdict scoring (dataset-ingestion Requirement 3.14)
# ---------------------------------------------------------------------------


def _assess_needs_ai(page: LabeledPage) -> bool:
    """Whether scoring this page's verdict would make an AI call.

    ``viability.assess()`` short-circuits in the free rule-based pre-scan for
    blocker pages (empty shells, challenge pages, login walls), returning
    ``wont_work`` with no AI call. Every other page reaches
    ``extraction.build_plan``, which calls the model. We detect the no-AI case
    by running the same pre-scan the orchestrator runs; a page the pre-scan
    blocks is deterministically scorable offline.
    """
    from app.ingestion import prescan

    return prescan.prescan(page.html) is None


def score_verdict_page(page: LabeledPage, *, include_ai: bool) -> VerdictPageScore:
    """Score one page's viability verdict against its labelled verdict.

    Runs :func:`app.ingestion.viability.assess` on the page's HTML (using a
    no-restriction robots result — robots is a warning only and never changes
    the label, Requirement 3.12) and compares the resulting ``.verdict`` to the
    page's hand-checked label. The AI-backed assessment runs only when
    ``include_ai`` is ``True``; offline, pages that would need an AI call are
    recorded as not scored, while blocker pages (pre-scan short-circuit, no AI)
    are scored deterministically. Mirrors the offline/live split used for the
    AI-backed extraction methods.
    """
    from app.ingestion.robots import RobotsResult
    from app.ingestion.viability import CaptureView, assess

    if not include_ai and _assess_needs_ai(page):
        return VerdictPageScore(
            page=page.name,
            expected=page.verdict,
            scored=False,
            note="skipped (no live AI)",
        )

    capture = CaptureView(html=page.html, page_title="", main_status=200)
    verdict, _plan = assess(capture, final_url=page.url, robots=RobotsResult(allowed=True))
    return VerdictPageScore(
        page=page.name,
        expected=page.verdict,
        predicted=verdict.verdict,
        scored=True,
    )


def score_verdicts(pages: list[LabeledPage], *, include_ai: bool) -> list[VerdictPageScore]:
    """Score the viability verdict for every page (R3.14)."""
    return [score_verdict_page(page, include_ai=include_ai) for page in pages]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def score_page(page: LabeledPage, *, include_ai: bool) -> PageScore:
    """Score every method on one page.

    Offline methods (``structured``, ``selectors``) always run. The AI-backed
    methods (``ai_direct``, ``auto``) run only when ``include_ai`` is ``True``;
    otherwise they are recorded as not-applicable with a note.
    """
    score = PageScore(page=page.name, verdict=page.verdict, tags=page.tags)
    score.methods[METHOD_STRUCTURED] = _run_structured(page)
    score.methods[METHOD_SELECTORS] = _run_selectors(page)

    if include_ai:
        score.methods[METHOD_AI_DIRECT] = _run_ai_direct(page, _TokenCounter())
        score.methods[METHOD_AUTO] = _run_auto(page, _TokenCounter())
    else:
        for method in (METHOD_AI_DIRECT, METHOD_AUTO):
            score.methods[method] = MethodScore(
                method=method,
                applicable=False,
                note="skipped (no live AI)",
                expected=len(page.reviews),
            )
    return score


def score_pages(pages: list[LabeledPage], *, include_ai: bool) -> Report:
    """Score every page and return the full :class:`Report`."""
    report = Report(include_ai=include_ai)
    for page in pages:
        report.pages.append(score_page(page, include_ai=include_ai))
    report.verdicts = score_verdicts(pages, include_ai=include_ai)
    return report


# ---------------------------------------------------------------------------
# Aggregation and thresholds
# ---------------------------------------------------------------------------


def aggregate_method(report: Report, method: str) -> MethodScore:
    """Micro-average a method's scores across all applicable pages.

    Precision/recall are computed from summed matched/extracted/expected counts
    (micro-averaging), and field accuracy from summed correct/total, so pages
    with more reviews weigh proportionally. Tokens and seconds are summed.
    """
    agg = MethodScore(method=method)
    rating_c = rating_t = date_c = date_t = author_c = author_t = 0
    np_total = np_correct = 0
    any_applicable = False
    for page in report.pages:
        ms = page.methods.get(method)
        if ms is None or not ms.applicable:
            continue
        any_applicable = True
        agg.expected += ms.expected
        agg.extracted += ms.extracted
        agg.matched_expected += ms.matched_expected
        agg.matched_extracted += ms.matched_extracted
        rating_c += ms.rating.correct
        rating_t += ms.rating.total
        date_c += ms.date.correct
        date_t += ms.date.total
        author_c += ms.author.correct
        author_t += ms.author.total
        agg.tokens += ms.tokens
        agg.seconds += ms.seconds
        if ms.next_page_correct is not None:
            np_total += 1
            np_correct += int(ms.next_page_correct)
    agg.applicable = any_applicable
    agg.rating = FieldAccuracy(rating_c, rating_t)
    agg.date = FieldAccuracy(date_c, date_t)
    agg.author = FieldAccuracy(author_c, author_t)
    agg.next_page_correct = (np_correct == np_total) if np_total else None
    return agg


def auto_meets_thresholds(report: Report) -> tuple[bool, float | None, float | None]:
    """Check the automatic choice against the Requirement 8.3 thresholds.

    Micro-averages precision and recall of the ``auto`` method over pages
    labelled ``will_work`` and compares them to :data:`AUTO_PRECISION_THRESHOLD`
    (0.98) and :data:`AUTO_RECALL_THRESHOLD` (0.90).

    Returns ``(passed, precision, recall)``. When the ``auto`` method was not
    scored (an offline run), precision/recall are ``None`` and ``passed`` is
    ``False`` — a threshold cannot be met without a live run. This computes and
    reports the thresholds; the failing CI gate that acts on them is Task 9.2.
    """
    extracted = matched_extracted = expected = matched_expected = 0
    scored = False
    for page in report.pages:
        if page.verdict != "will_work":
            continue
        ms = page.methods.get(METHOD_AUTO)
        if ms is None or not ms.applicable:
            continue
        scored = True
        extracted += ms.extracted
        matched_extracted += ms.matched_extracted
        expected += ms.expected
        matched_expected += ms.matched_expected

    if not scored:
        return False, None, None

    precision = (matched_extracted / extracted) if extracted else None
    recall = (matched_expected / expected) if expected else None
    passed = (
        precision is not None
        and recall is not None
        and precision >= AUTO_PRECISION_THRESHOLD
        and recall >= AUTO_RECALL_THRESHOLD
    )
    return passed, precision, recall


def aggregate_verdict(report: Report) -> VerdictScore:
    """Aggregate verdict accuracy over the pages that were scored this run.

    Counts a page only when its verdict was scored (``scored=True``): offline,
    that is the blocker subset; live, it is every page. ``correct`` is the
    number of scored pages whose predicted verdict equalled the labelled one.
    """
    correct = sum(1 for v in report.verdicts if v.correct)
    total = sum(1 for v in report.verdicts if v.scored)
    return VerdictScore(correct=correct, total=total)


def verdict_meets_threshold(report: Report) -> tuple[bool, float | None]:
    """Check verdict accuracy against the Requirement 3.14 threshold (0.90).

    Returns ``(passed, accuracy)``. The full-corpus gate requires the live run:
    on an offline run the AI-backed verdicts were not scored, so ``accuracy`` is
    ``None`` and ``passed`` is ``False`` — the threshold cannot be met without
    the live run. This mirrors :func:`auto_meets_thresholds`, which returns
    ``passed=False`` / ``None`` offline. The offline-scorable subset (blocker
    pages) is still reported via :func:`aggregate_verdict` for visibility, but it
    does not satisfy the full-corpus gate on its own.
    """
    if not report.include_ai:
        return False, None

    accuracy = aggregate_verdict(report).accuracy
    passed = accuracy is not None and accuracy >= VERDICT_ACCURACY_THRESHOLD
    return passed, accuracy
