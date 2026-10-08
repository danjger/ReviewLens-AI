"""Unit tests for app.handlers.metrics (review-analysis task 5).

These cover the metrics-and-output stage: the metric math (count, average
rating, rating distribution, date range, sentiment breakdown, reported total,
and the extraction details), the shape of ``reviews/v{n}.json`` with minted
``r_0001`` ids, and the idempotent S3 write (writing the same version twice
overwrites, producing identical bytes and no duplicate object).

The datasets-without-ratings and datasets-without-dates cases are checked
explicitly (Requirement 5.1: average/distribution and date range exist only
"when ratings/dates exist").

_Validates: Requirements 3.5, 5.1, 5.4, 5.5, 5.6, 7.3_
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import boto3
import pytest
from app.core.config import get_settings
from app.extraction.models import VerifiedReview
from app.handlers import metrics
from app.handlers.extraction_stage import CollectedReview, PageDetail
from app.storage import keys
from app.storage import s3 as s3_mod
from app.worker.ai.profile import EntityProfile
from app.worker.ai.sentiment import Sentiment
from app.worker.ai.themes import Theme
from moto import mock_aws

_REGION = "us-east-1"
_BUCKET = "reviewlens-metrics-test"
_DATASET = "ds-metrics"
_VERSION = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _review(
    text: str,
    *,
    rating: float | None = None,
    date: str | None = None,
    author: str | None = None,
    title: str | None = None,
    page: int = 1,
) -> CollectedReview:
    return CollectedReview(
        review=VerifiedReview(text=text, rating=rating, date=date, author=author, title=title),
        source_page=page,
    )


def _page(
    num: int,
    *,
    method: str = "selectors",
    found: int = 1,
    fallback: bool = False,
    discarded: int = 0,
    reported_total: int | None = None,
    structured_agreement: float | None = None,
) -> PageDetail:
    return PageDetail(
        page=num,
        url=f"https://reviews.example.com/p/{num}",
        method=method,  # type: ignore[arg-type]
        found=found,
        fallback=fallback,
        discarded=discarded,
        reported_total=reported_total,
        structured_agreement=structured_agreement,
    )


def _profile() -> EntityProfile:
    return EntityProfile(
        name="Acme CRM", category="CRM software", description="A CRM.", confidence="high"
    )


def _compute(
    reviews: list[CollectedReview],
    *,
    sentiments: list[Sentiment] | None = None,
    themes: list[Theme] | None = None,
    pages: list[PageDetail] | None = None,
    skipped: int = 0,
    warnings: list[str] | None = None,
    duration_ms: int = 0,
) -> metrics.MetricsResult:
    labels: list[Sentiment] = sentiments if sentiments is not None else ["neutral"] * len(reviews)
    return metrics.compute_metrics(
        dataset_id=_DATASET,
        version=_VERSION,
        reviews=reviews,
        sentiments=labels,
        themes=themes or [],
        profile=_profile(),
        pages=pages if pages is not None else [],
        skipped=skipped,
        warnings=warnings or [],
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# Review ids
# ---------------------------------------------------------------------------


class TestReviewIds:
    def test_ids_are_r_prefixed_and_1_based_zero_padded(self) -> None:
        assert metrics.review_id(0) == "r_0001"
        assert metrics.review_id(1) == "r_0002"
        assert metrics.review_id(211) == "r_0212"

    def test_doc_mints_ids_in_review_order(self) -> None:
        result = _compute([_review("a"), _review("b"), _review("c")])
        ids = [r["id"] for r in result.reviews_doc["reviews"]]
        assert ids == ["r_0001", "r_0002", "r_0003"]


# ---------------------------------------------------------------------------
# Count
# ---------------------------------------------------------------------------


class TestReviewCount:
    def test_count_equals_number_of_reviews(self) -> None:
        result = _compute([_review("a"), _review("b")])
        assert result.metrics["review_count"] == 2
        assert result.review_count == 2

    def test_empty_is_zero(self) -> None:
        result = _compute([])
        assert result.metrics["review_count"] == 0
        assert result.reviews_doc["reviews"] == []


# ---------------------------------------------------------------------------
# Average rating + distribution
# ---------------------------------------------------------------------------


class TestRatings:
    def test_average_rounded_to_one_decimal(self) -> None:
        reviews = [_review("a", rating=4), _review("b", rating=5), _review("c", rating=4)]
        # mean = 13/3 = 4.333... -> 4.3
        assert _compute(reviews).metrics["avg_rating"] == 4.3

    def test_distribution_counts_each_whole_star(self) -> None:
        reviews = [
            _review("a", rating=1),
            _review("b", rating=4),
            _review("c", rating=4),
            _review("d", rating=5),
        ]
        dist = _compute(reviews).metrics["rating_distribution"]
        assert dist == {"1": 1, "2": 0, "3": 0, "4": 2, "5": 1}

    def test_distribution_sums_to_rated_count(self) -> None:
        reviews = [_review("a", rating=2), _review("b", rating=5), _review("c")]  # one unrated
        m = _compute(reviews).metrics
        assert sum(m["rating_distribution"].values()) == 2  # only the two rated

    def test_fractional_rating_rounds_into_bucket(self) -> None:
        # A 4.5-scale value rounds to the nearest whole star and clamps 1..5.
        reviews = [_review("a", rating=3.5), _review("b", rating=0.4), _review("c", rating=9)]
        dist = _compute(reviews).metrics["rating_distribution"]
        # 3.5 -> 4 (banker's rounding), 0.4 -> 1 (clamped), 9 -> 5 (clamped)
        assert dist == {"1": 1, "2": 0, "3": 0, "4": 1, "5": 1}

    def test_no_ratings_yields_none(self) -> None:
        # Requirement 5.1: average/distribution exist only when ratings exist.
        m = _compute([_review("a"), _review("b")]).metrics
        assert m["avg_rating"] is None
        assert m["rating_distribution"] is None


# ---------------------------------------------------------------------------
# Date range
# ---------------------------------------------------------------------------


class TestDateRange:
    def test_min_and_max_of_dates(self) -> None:
        reviews = [
            _review("a", date="2024-02-01"),
            _review("b", date="2026-09-20"),
            _review("c", date="2025-01-15"),
        ]
        assert _compute(reviews).metrics["date_range"] == {
            "min": "2024-02-01",
            "max": "2026-09-20",
        }

    def test_ignores_reviews_without_dates(self) -> None:
        reviews = [_review("a", date="2025-05-05"), _review("b")]
        assert _compute(reviews).metrics["date_range"] == {
            "min": "2025-05-05",
            "max": "2025-05-05",
        }

    def test_no_dates_yields_none(self) -> None:
        # Requirement 5.1: earliest/latest exist only when dates exist.
        assert _compute([_review("a"), _review("b")]).metrics["date_range"] is None


# ---------------------------------------------------------------------------
# Sentiment breakdown
# ---------------------------------------------------------------------------


class TestSentiment:
    def test_counts_each_label_with_all_keys_present(self) -> None:
        reviews = [_review("a"), _review("b"), _review("c")]
        sentiments: list[Sentiment] = ["positive", "positive", "negative"]
        m = _compute(reviews, sentiments=sentiments).metrics
        assert m["sentiment"] == {"positive": 2, "neutral": 0, "negative": 1}

    def test_breakdown_sums_to_review_count(self) -> None:
        reviews = [_review("a"), _review("b"), _review("c"), _review("d")]
        sentiments: list[Sentiment] = ["positive", "neutral", "negative", "positive"]
        m = _compute(reviews, sentiments=sentiments).metrics
        assert sum(m["sentiment"].values()) == m["review_count"]

    def test_missing_tail_labels_default_neutral(self) -> None:
        # Defensive: fewer labels than reviews -> missing tail counted neutral,
        # so the breakdown still sums to the review count.
        reviews = [_review("a"), _review("b"), _review("c")]
        m = _compute(reviews, sentiments=["positive"]).metrics
        assert m["sentiment"] == {"positive": 1, "neutral": 2, "negative": 0}
        assert sum(m["sentiment"].values()) == 3

    def test_label_written_onto_each_review(self) -> None:
        reviews = [_review("a"), _review("b")]
        doc = _compute(reviews, sentiments=["negative", "positive"]).reviews_doc
        assert [r["sentiment"] for r in doc["reviews"]] == ["negative", "positive"]


# ---------------------------------------------------------------------------
# Reported total (5.4) + pages + skipped
# ---------------------------------------------------------------------------


class TestReportedTotalAndCounts:
    def test_reported_total_is_first_page_value(self) -> None:
        pages = [_page(1, reported_total=1540), _page(2, reported_total=1540)]
        assert _compute([_review("a")], pages=pages).metrics["reported_total"] == 1540

    def test_reported_total_skips_pages_without_one(self) -> None:
        pages = [_page(1, reported_total=None), _page(2, reported_total=88)]
        assert _compute([_review("a")], pages=pages).metrics["reported_total"] == 88

    def test_reported_total_none_when_no_page_shows_one(self) -> None:
        pages = [_page(1), _page(2)]
        assert _compute([_review("a")], pages=pages).metrics["reported_total"] is None

    def test_reported_total_none_for_upload(self) -> None:
        # Uploads have no pages.
        assert _compute([_review("a", page=0)], pages=[]).metrics["reported_total"] is None

    def test_pages_captured_and_skipped(self) -> None:
        pages = [_page(1), _page(2), _page(3)]
        m = _compute([_review("a")], pages=pages, skipped=7).metrics
        assert m["pages_captured"] == 3
        assert m["skipped"] == 7


# ---------------------------------------------------------------------------
# Extraction details (5.6)
# ---------------------------------------------------------------------------


class TestExtractionDetails:
    def test_counts_pages_by_method_and_sums_discards(self) -> None:
        pages = [
            _page(1, method="selectors", discarded=2),
            _page(2, method="structured", discarded=1),
            _page(3, method="ai_direct", fallback=True, discarded=3),
        ]
        ex = _compute([_review("a")], pages=pages).metrics["extraction"]
        assert ex["method"] == "selectors"  # first page's method
        assert ex["pages_by_selectors"] == 2  # selectors + structured
        assert ex["pages_by_ai"] == 1
        assert ex["locator_discarded"] == 6

    def test_structured_agreement_first_applicable_page(self) -> None:
        pages = [
            _page(1, structured_agreement=None),
            _page(2, structured_agreement=0.95),
        ]
        ex = _compute([_review("a")], pages=pages).metrics["extraction"]
        assert ex["structured_agreement"] == 0.95

    def test_structured_agreement_none_when_no_page_has_it(self) -> None:
        ex = _compute([_review("a")], pages=[_page(1), _page(2)]).metrics["extraction"]
        assert ex["structured_agreement"] is None

    def test_method_is_upload_when_no_pages(self) -> None:
        ex = _compute([_review("a", page=0)], pages=[]).metrics["extraction"]
        assert ex["method"] == "upload"
        assert ex["pages_by_selectors"] == 0
        assert ex["pages_by_ai"] == 0


# ---------------------------------------------------------------------------
# Themes + warnings + duration
# ---------------------------------------------------------------------------


class TestThemesWarningsDuration:
    def test_themes_rendered_as_label_mentions_lean(self) -> None:
        themes = [
            Theme(label="Support", mentions=44, lean="negative", example_ids=["0", "2"]),
            Theme(label="Price", mentions=12, lean="positive", example_ids=[]),
        ]
        reviews = [_review("a"), _review("b"), _review("c")]
        m = _compute(reviews, themes=themes).metrics
        assert m["themes"] == [
            {"label": "Support", "mentions": 44, "lean": "negative"},
            {"label": "Price", "mentions": 12, "lean": "positive"},
        ]

    def test_warnings_and_duration_passed_through(self) -> None:
        m = _compute([_review("a")], warnings=["w1", "w2"], duration_ms=48210).metrics
        assert m["warnings"] == ["w1", "w2"]
        assert m["duration_ms"] == 48210


# ---------------------------------------------------------------------------
# reviews/v{n}.json shape
# ---------------------------------------------------------------------------


class TestReviewsDocShape:
    def test_top_level_shape(self) -> None:
        doc = _compute([_review("a")], pages=[_page(1)]).reviews_doc
        assert doc["dataset_id"] == _DATASET
        assert doc["version"] == _VERSION
        assert "generated_at" in doc
        assert set(doc["entity"]) == {"name", "category", "description", "confidence"}
        assert doc["entity"]["name"] == "Acme CRM"

    def test_pages_mirror_details(self) -> None:
        pages = [_page(1, method="selectors", found=24, fallback=False, discarded=0)]
        doc = _compute([_review("a")], pages=pages).reviews_doc
        assert doc["pages"] == [
            {
                "page": 1,
                "url": "https://reviews.example.com/p/1",
                "method": "selectors",
                "found": 24,
                "fallback": False,
                "discarded": 0,
            }
        ]

    def test_review_entry_carries_all_fields(self) -> None:
        reviews = [
            _review("Great", rating=4, date="2026-07-01", author="J. D.", title="Loved it", page=2)
        ]
        doc = _compute(reviews, sentiments=["positive"]).reviews_doc
        assert doc["reviews"][0] == {
            "id": "r_0001",
            "text": "Great",
            "rating": 4,
            "date": "2026-07-01",
            "author": "J. D.",
            "title": "Loved it",
            "sentiment": "positive",
            "source_page": 2,
        }


# ---------------------------------------------------------------------------
# Idempotent S3 write (7.3)
# ---------------------------------------------------------------------------


@pytest.fixture()
def s3_bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_BUCKET", _BUCKET)
    get_settings.cache_clear()
    s3_mod.reset_client()
    with mock_aws():
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
        yield
    s3_mod.reset_client()
    get_settings.cache_clear()


class TestWrite:
    def test_write_round_trips_to_reviews_key(self, s3_bucket: None) -> None:
        result = _compute([_review("a", rating=5)], pages=[_page(1)])
        key = metrics.write_reviews(_DATASET, _VERSION, result.reviews_doc)

        assert key == keys.dataset_reviews(_DATASET, _VERSION)
        stored = json.loads(s3_mod.get_text(key))
        assert stored == result.reviews_doc

    def test_run_computes_and_writes(self, s3_bucket: None) -> None:
        result = metrics.run(
            dataset_id=_DATASET,
            version=_VERSION,
            reviews=[_review("a")],
            sentiments=["neutral"],
            themes=[],
            profile=_profile(),
            pages=[_page(1)],
        )
        stored = json.loads(s3_mod.get_text(keys.dataset_reviews(_DATASET, _VERSION)))
        assert stored == result.reviews_doc

    def test_rewriting_same_version_overwrites_identically(self, s3_bucket: None) -> None:
        # Requirement 7.3: reprocessing the same version overwrites that
        # version's object in place; no duplicate, and (generated_at aside) the
        # payload is identical.
        reviews = [_review("a", rating=4), _review("b", rating=2)]
        doc1 = _compute(reviews, pages=[_page(1)]).reviews_doc
        key = metrics.write_reviews(_DATASET, _VERSION, doc1)
        metrics.write_reviews(_DATASET, _VERSION, doc1)

        client = boto3.client("s3", region_name=_REGION)
        listed = client.list_objects_v2(Bucket=_BUCKET, Prefix=key)
        assert listed["KeyCount"] == 1  # one object, overwritten not duplicated
        assert json.loads(s3_mod.get_text(key)) == doc1
