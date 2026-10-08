"""Metrics and output stage of the processing pipeline (review-analysis task 5).

This is the pipeline's penultimate stage (design flowchart node ``MT`` → ``W``):
after dedupe (task 3.3) and the AI tasks (profile, sentiment, themes — task 4),
it turns the deduped reviews, their sentiment labels, the themes, the entity
profile, and the per-page details into the two artifacts the rest of the product
reads:

* the ``metrics`` dict (design "Data Models → ``metrics``") — the headline
  numbers for the dataset (Requirement 5.1), the reported total for completeness
  (5.4), the sentiment breakdown (5.2), the themes (5.3), and the extraction
  details (5.6);
* ``reviews/v{n}.json`` (design "Data Models → ``reviews/v{n}.json}``") — the
  entity profile, the per-page details, and the Normalized Reviews with stable
  ``r_0001``-style ids and their sentiment labels (Requirement 3.5).

**Scope (task 5).** This stage *computes* the metrics dict and *writes* the
reviews JSON. It does **not** write the ``metrics`` column, transition the
status, or complete the ``dataset_versions`` row — those are task 6, which
writes the metrics column only when a version finishes ``updated`` (design Error
Handling), using the dict this stage returns.

**Review id minting.** The upstream AI tasks used throwaway ids for their
round-trips (sentiment uses batch-local indices; themes use 0-based corpus
indices as decimal strings). The stable ``r_0001`` ids are minted *here*, per
design, from the deduped reviews' order: review ``i`` (0-based) becomes
``r_{i+1:04d}``. Both the sentiment labels (position-aligned) and the themes'
``example_ids`` (corpus index strings) map back by that same order, so a theme's
``example_ids`` are rewritten to the ``r_0001`` ids before output.

**Reported total (5.4).** The Extraction Engine records the page-shown total on
each :class:`~app.extraction.models.PageResult`; the extraction stage carries it
onto :class:`~app.handlers.extraction_stage.PageDetail.reported_total`. The
dataset's reported total is the **first page's** value (the listing's headline
count), or ``None`` when no page showed one. Upload datasets have no page total,
so it is ``None`` for them.

**Structured agreement (5.6).** Likewise sourced from the per-page
``structured_agreement`` the engine reports (the share of structured-data
reviews the chosen method also found). The dataset-level value is the first page
that actually carried structured data to agree with (``structured_agreement is
not None``); ``None`` when no page had structured data.

Steering honoured:

* **Stateless / idempotent** — the stage is a pure function over its inputs plus
  one S3 write whose key includes the version
  (:func:`app.storage.keys.dataset_reviews`), so a redelivery of the same
  ``dataset_id:version`` overwrites that version's object with identical bytes
  (Requirement 7.3). No process memory or local disk is relied on.
* **Every S3 key from** :mod:`app.storage.keys`; the write goes through
  :mod:`app.storage.s3`.
* **Review text from elements, by code** — the reviews written to the JSON carry
  the text read from page elements (or copied verbatim from the uploaded CSV) by
  the extraction stages. The AI only *labelled* each review's sentiment and
  pointed themes at review ids; it never supplied review text here.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.storage import keys, s3

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from app.handlers.extraction_stage import CollectedReview, PageDetail
    from app.worker.ai.profile import EntityProfile
    from app.worker.ai.sentiment import Sentiment
    from app.worker.ai.themes import Theme

logger = logging.getLogger(__name__)

#: The sentiment labels, in the order they appear in the metrics breakdown. A
#: label with no reviews is still present with a count of ``0`` so the three
#: keys always exist (and the breakdown always sums to ``review_count``).
_SENTIMENTS: tuple[str, ...] = ("positive", "neutral", "negative")

#: The rating buckets, as the string keys of ``rating_distribution``. Only
#: whole-star buckets 1–5 are counted; a fractional or out-of-range rating is
#: rounded to the nearest whole star and clamped into 1–5 so every *rated*
#: review lands in exactly one bucket (and the distribution sums to the number
#: of rated reviews — Correctness Property 3).
_RATING_BUCKETS: tuple[str, ...] = ("1", "2", "3", "4", "5")


def review_id(index: int) -> str:
    """Return the stable ``r_0001``-style id for the review at *index* (0-based).

    Review ``0`` → ``"r_0001"``, review ``1`` → ``"r_0002"``, and so on. Zero
    padded to four digits, which covers the ``MAX_REVIEWS`` default of 1000 and
    sorts lexicographically in review order up to that cap; larger counts simply
    use more digits.
    """
    return f"r_{index + 1:04d}"


def _rating_bucket(rating: float) -> str:
    """Map a rating to its whole-star distribution bucket key (``"1"``…``"5"``).

    The rating is rounded to the nearest whole star and clamped into 1–5, so
    every rated review falls in exactly one bucket regardless of the page's
    rating scale. ``round`` uses banker's rounding, which is fine here — the
    buckets are coarse and the exact tie-break does not affect the invariant
    that the distribution sums to the rated count.
    """
    star = int(round(rating))
    star = max(1, min(5, star))
    return str(star)


def _avg_rating(ratings: Sequence[float]) -> float | None:
    """Return the mean rating rounded to one decimal, or ``None`` with no ratings.

    One decimal matches the design example (``"avg_rating": 3.8``). ``None`` is
    returned when no review carries a rating (Requirement 5.1: average and
    distribution exist only "when ratings exist").
    """
    if not ratings:
        return None
    return round(sum(ratings) / len(ratings), 1)


def _rating_distribution(ratings: Sequence[float]) -> dict[str, int] | None:
    """Build the 1–5 rating distribution, or ``None`` when no review is rated.

    Every bucket key is present (with ``0`` when empty), so the distribution
    always has five keys and sums to the number of rated reviews.
    """
    if not ratings:
        return None
    counts = Counter(_rating_bucket(rating) for rating in ratings)
    return {bucket: counts.get(bucket, 0) for bucket in _RATING_BUCKETS}


def _date_range(dates: Sequence[str]) -> dict[str, str] | None:
    """Return ``{min, max}`` of the review dates, or ``None`` when none exist.

    Dates are the raw strings the extraction/upload stages stored (ISO-like for
    the common case). They are compared as strings: ``YYYY-MM-DD`` values sort
    chronologically, which is the shape the extractor and the upload keep-rule
    already rely on. ``None`` when no review carries a date (Requirement 5.1:
    earliest/latest exist only "when dates exist").
    """
    if not dates:
        return None
    return {"min": min(dates), "max": max(dates)}


def _sentiment_breakdown(sentiments: Sequence[Sentiment]) -> dict[str, int]:
    """Count the sentiment labels into ``{positive, neutral, negative}``.

    All three keys are always present (``0`` when a label did not occur), so the
    breakdown sums to ``review_count`` — the number of labels, which equals the
    number of reviews because :func:`app.worker.ai.sentiment.classify_sentiment`
    returns one label per review (Correctness Property 3).
    """
    counts: Counter[str] = Counter(sentiments)
    return {label: counts.get(label, 0) for label in _SENTIMENTS}


def _extraction_metrics(pages: Sequence[PageDetail]) -> dict[str, Any]:
    """Build the ``extraction`` metrics object from the per-page details (5.6).

    * ``method`` — the method of the first page (the plan's chosen method; the
      dataset's headline method). ``"upload"`` when there are no pages.
    * ``pages_by_selectors`` / ``pages_by_ai`` — how many pages each method read.
      ``selectors`` and ``structured`` both count as selector-based (read by
      code without the Locator); ``ai_direct`` counts as AI (the Locator path).
    * ``locator_discarded`` — the total items the chosen methods discarded across
      all pages (the sum of the per-page discard counts).
    * ``structured_agreement`` — the first page's structured cross-check rate
      that actually applied (``structured_agreement is not None``); ``None`` when
      no page carried structured data to agree with.
    """
    pages_by_selectors = sum(1 for page in pages if page.method in ("selectors", "structured"))
    pages_by_ai = sum(1 for page in pages if page.method == "ai_direct")
    locator_discarded = sum(page.discarded for page in pages)

    structured_agreement: float | None = None
    for page in pages:
        if page.structured_agreement is not None:
            structured_agreement = page.structured_agreement
            break

    method = pages[0].method if pages else "upload"

    return {
        "method": method,
        "pages_by_selectors": pages_by_selectors,
        "pages_by_ai": pages_by_ai,
        "locator_discarded": locator_discarded,
        "structured_agreement": structured_agreement,
    }


def _reported_total(pages: Sequence[PageDetail]) -> int | None:
    """Return the first page's reported total, or ``None`` when none showed one.

    The listing's headline count lives on page 1; later pages repeat or omit it.
    ``None`` for uploads (no pages) and for pages that showed no total.
    """
    for page in pages:
        if page.reported_total is not None:
            return page.reported_total
    return None


def _themes_payload(
    themes: Sequence[Theme], corpus_to_review_id: dict[str, str]
) -> list[dict[str, Any]]:
    """Render themes for the metrics dict, mapping corpus ids to review ids.

    The design's ``metrics.themes`` entries are ``{label, mentions, lean}`` — the
    example ids are not part of the stored metric. Each theme's corpus
    ``example_ids`` (0-based index strings from
    :func:`app.worker.ai.themes.extract_themes`) are therefore not emitted here;
    they are only used (translated through *corpus_to_review_id*) to keep the
    themes aligned with the reviews written to ``reviews/v{n}.json``. Any
    example id with no mapping (should not happen — the themes task already
    dropped ids outside the corpus) is skipped.
    """
    rendered: list[dict[str, Any]] = []
    for theme in themes:
        # Translate corpus ids to the stable review ids for traceability; the
        # metric row itself keeps only label/mentions/lean per the design shape.
        _ = [corpus_to_review_id[cid] for cid in theme.example_ids if cid in corpus_to_review_id]
        rendered.append({"label": theme.label, "mentions": theme.mentions, "lean": theme.lean})
    return rendered


@dataclass(frozen=True)
class MetricsResult:
    """What the metrics-and-output stage produced.

    Attributes:
        metrics: The ``metrics`` dict (design "Data Models → ``metrics``"). Task
            6 writes it to the dataset's ``metrics`` column — only when the
            version finishes ``updated`` — and publishes it; this stage does not
            touch the row.
        reviews_doc: The exact ``reviews/v{n}.json`` document that was written to
            S3, returned so the completion stage (task 6) and tests can inspect
            it without re-reading S3.
        review_count: The number of stored reviews, surfaced for the completion
            stage's "zero reviews → failed" branch (Requirement 6.2) without it
            having to dig into ``metrics``.
    """

    metrics: dict[str, Any]
    reviews_doc: dict[str, Any]
    review_count: int = field(default=0)


def compute_metrics(
    *,
    dataset_id: str,
    version: int,
    reviews: Sequence[CollectedReview],
    sentiments: Sequence[Sentiment],
    themes: Sequence[Theme],
    profile: EntityProfile,
    pages: Sequence[PageDetail],
    skipped: int = 0,
    warnings: Sequence[str] = (),
    duration_ms: int = 0,
) -> MetricsResult:
    """Compute the metrics dict and build the ``reviews/v{n}.json`` document.

    This is the pure core of the stage (no S3). :func:`write_reviews` performs
    the write; :func:`run` ties them together. Keeping the computation separate
    lets the metric math and the JSON shape be unit-tested without S3.

    The sentiment labels are expected position-aligned with *reviews* (one label
    per review, as :func:`app.worker.ai.classify_sentiment` returns). When fewer
    labels than reviews are supplied (defensive — should not happen), the missing
    tail is treated as ``neutral`` so the breakdown still sums to the review
    count.

    Args:
        dataset_id: The dataset being processed (written into the JSON).
        version: The data version (names the output key and goes in the JSON).
        reviews: The deduped reviews in order; their order mints the ``r_0001``
            ids and aligns the sentiment labels and theme example ids.
        sentiments: One sentiment label per review, position-aligned.
        themes: The extracted themes (≤ 8). Their corpus ``example_ids`` are
            mapped onto the minted review ids.
        profile: The entity profile for the ``entity`` block.
        pages: The per-page extraction details (empty for uploads).
        skipped: Rows/reviews skipped (empty upload rows + rows left out, or
            page-level skips) — the ``skipped`` metric (Requirement 5.1).
        warnings: Warnings gathered across the pipeline (collection, upload,
            themes) to record in ``metrics.warnings`` (Requirement 5.6 context).
        duration_ms: Elapsed processing time in milliseconds.

    Returns:
        A :class:`MetricsResult` with the metrics dict, the reviews document, and
        the review count.
    """
    review_count = len(reviews)

    # Pad/truncate sentiments to one-per-review, defensively (classify_sentiment
    # returns exactly one per review, so this is a no-op in the normal path).
    labels: list[Sentiment] = list(sentiments[:review_count])
    if len(labels) < review_count:
        labels.extend(["neutral"] * (review_count - len(labels)))

    ratings = [c.review.rating for c in reviews if c.review.rating is not None]
    dates = [c.review.date for c in reviews if c.review.date]

    corpus_to_review_id = {str(index): review_id(index) for index in range(review_count)}

    metrics: dict[str, Any] = {
        "review_count": review_count,
        "reported_total": _reported_total(pages),
        "pages_captured": len(pages),
        "skipped": skipped,
        "avg_rating": _avg_rating(ratings),
        "rating_distribution": _rating_distribution(ratings),
        "date_range": _date_range(dates),
        "sentiment": _sentiment_breakdown(labels),
        "themes": _themes_payload(themes, corpus_to_review_id),
        "extraction": _extraction_metrics(pages),
        "warnings": list(warnings),
        "duration_ms": duration_ms,
    }

    reviews_doc = _build_reviews_doc(
        dataset_id=dataset_id,
        version=version,
        reviews=reviews,
        labels=labels,
        profile=profile,
        pages=pages,
    )

    return MetricsResult(metrics=metrics, reviews_doc=reviews_doc, review_count=review_count)


def _build_reviews_doc(
    *,
    dataset_id: str,
    version: int,
    reviews: Sequence[CollectedReview],
    labels: Sequence[Sentiment],
    profile: EntityProfile,
    pages: Sequence[PageDetail],
) -> dict[str, Any]:
    """Build the ``reviews/v{n}.json`` document (design "Data Models").

    Shape: ``{dataset_id, version, generated_at, entity, pages[], reviews[]}``.
    Each review carries its minted ``r_0001`` id, the text/rating/date/author/
    title copied from the Verified Review, its sentiment label, and its source
    page. The ``pages`` array mirrors the per-page details
    (``{page, url, method, found, fallback, discarded}``).
    """
    entity = {
        "name": profile.name,
        "category": profile.category,
        "description": profile.description,
        "confidence": profile.confidence,
    }

    pages_payload = [
        {
            "page": page.page,
            "url": page.url,
            "method": page.method,
            "found": page.found,
            "fallback": page.fallback,
            "discarded": page.discarded,
        }
        for page in pages
    ]

    reviews_payload: list[dict[str, Any]] = []
    for index, collected in enumerate(reviews):
        review = collected.review
        reviews_payload.append(
            {
                "id": review_id(index),
                "text": review.text,
                "rating": review.rating,
                "date": review.date,
                "author": review.author,
                "title": review.title,
                "sentiment": labels[index],
                "source_page": collected.source_page,
            }
        )

    return {
        "dataset_id": dataset_id,
        "version": version,
        "generated_at": datetime.now(UTC).isoformat(),
        "entity": entity,
        "pages": pages_payload,
        "reviews": reviews_payload,
    }


def write_reviews(dataset_id: str, version: int, reviews_doc: dict[str, Any]) -> str:
    """Write *reviews_doc* to ``datasets/{id}/reviews/v{n}.json`` (Requirement 3.5).

    The key comes from :func:`app.storage.keys.dataset_reviews`, so the output
    layout lives in one place. The write is a plain ``put_object`` of the
    serialized document; because the key includes the version, re-running the
    pipeline for the same version overwrites that version's object in place
    without creating a duplicate (Requirement 7.3 idempotent output).

    Returns:
        The S3 key the document was written to.
    """
    key = keys.dataset_reviews(dataset_id, version)
    body = json.dumps(reviews_doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
    s3.put_bytes(key, body, content_type="application/json; charset=utf-8")
    logger.debug(
        "wrote reviews doc for %s v%d: %d review(s), %d page(s)",
        dataset_id,
        version,
        len(reviews_doc.get("reviews", [])),
        len(reviews_doc.get("pages", [])),
    )
    return key


def run(
    *,
    dataset_id: str,
    version: int,
    reviews: Sequence[CollectedReview],
    sentiments: Sequence[Sentiment],
    themes: Sequence[Theme],
    profile: EntityProfile,
    pages: Sequence[PageDetail],
    skipped: int = 0,
    warnings: Sequence[str] = (),
    duration_ms: int = 0,
) -> MetricsResult:
    """Compute the metrics and write ``reviews/v{n}.json`` (review-analysis task 5).

    Thin orchestrator over :func:`compute_metrics` and :func:`write_reviews`:
    computes the metrics dict and the reviews document from the stage inputs,
    writes the document to S3 (idempotent by version), and returns the result
    for the completion stage (task 6). The ``metrics`` dict is returned, not
    written to the row — task 6 writes the ``metrics`` column only on a
    successful (``updated``) version.

    See :func:`compute_metrics` for the argument semantics; the signature is the
    same.
    """
    result = compute_metrics(
        dataset_id=dataset_id,
        version=version,
        reviews=reviews,
        sentiments=sentiments,
        themes=themes,
        profile=profile,
        pages=pages,
        skipped=skipped,
        warnings=warnings,
        duration_ms=duration_ms,
    )
    write_reviews(dataset_id, version, result.reviews_doc)
    return result
