"""Unit tests for app.handlers.upload_extraction (review-analysis task 3.2).

These cover the upload extraction stage: it reads the saved ``mapping.json`` and
the uploaded CSV from the dataset's own S3 objects, builds Normalized Reviews
from the column mapping, skips rows with empty review text (counting them), and
keeps at most ``MAX_REVIEWS`` rows using the keep rule shown in the upload
preview, recording a warning with the number of rows left out.

The CSV and mapping are stored in a moto-backed S3 bucket so the real
``keys.dataset_raw_upload`` / ``keys.dataset_raw_mapping`` keys and the S3 read
path are exercised; no AI, browser, or network is involved. ``MAX_REVIEWS`` is
pinned small (3) via the environment so the keep rule can be tested with tiny
fixture files.

_Validates: Requirement 3.4_
"""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest
from app.core.config import get_settings
from app.handlers import upload_extraction
from app.handlers.upload_extraction import UPLOAD_SOURCE_PAGE, extract_upload
from app.storage import keys
from app.storage import s3 as s3_mod
from moto import mock_aws

_BUCKET = "reviewlens-test"
_DATASET = "ds-upload-1"
_VERSION = 1


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def s3_bucket(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A moto-backed S3 bucket so ``extract_upload`` reads real objects.

    ``MAX_REVIEWS`` is pinned to 3 so the keep rule is exercised by small files.
    """
    monkeypatch.setenv("S3_BUCKET", _BUCKET)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "")
    monkeypatch.setenv("MAX_REVIEWS", "3")
    get_settings.cache_clear()
    s3_mod.reset_client()
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET)
        yield client
    get_settings.cache_clear()
    s3_mod.reset_client()


def _put_upload(
    client: Any,
    csv_text: str,
    mapping: dict[str, str],
    keep_rule: str,
    *,
    will_keep: int = 0,
) -> None:
    """Write the CSV and mapping.json for the dataset version under test."""
    client.put_object(
        Bucket=_BUCKET,
        Key=keys.dataset_raw_upload(_DATASET, _VERSION),
        Body=csv_text.encode("utf-8"),
    )
    doc = {"mapping": mapping, "keep_rule": keep_rule, "will_keep": will_keep}
    client.put_object(
        Bucket=_BUCKET,
        Key=keys.dataset_raw_mapping(_DATASET, _VERSION),
        Body=json.dumps(doc).encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Building reviews from the mapping
# ---------------------------------------------------------------------------


class TestBuildsReviews:
    """Normalized Reviews are built from the column mapping (Requirement 3.4)."""

    def test_maps_all_columns_verbatim(self, s3_bucket: Any) -> None:
        _put_upload(
            s3_bucket,
            "review,stars,when,who,headline\nGreat product,5,2026-01-02,Alice,Loved it\n",
            {
                "text": "review",
                "rating": "stars",
                "date": "when",
                "author": "who",
                "title": "headline",
            },
            "most_recent_by_date",
        )

        result = extract_upload(_DATASET, _VERSION)

        assert len(result.reviews) == 1
        review = result.reviews[0].review
        assert review.text == "Great product"
        assert review.rating == 5.0
        assert review.date == "2026-01-02"
        assert review.author == "Alice"
        assert review.title == "Loved it"
        # Uploads have no pages — source_page is the upload sentinel.
        assert result.reviews[0].source_page == UPLOAD_SOURCE_PAGE

    def test_optional_columns_absent_from_mapping_are_none(self, s3_bucket: Any) -> None:
        _put_upload(
            s3_bucket,
            "review\nSolid value\n",
            {"text": "review"},
            "first_in_file",
        )

        review = extract_upload(_DATASET, _VERSION).reviews[0].review

        assert review.text == "Solid value"
        assert review.rating is None
        assert review.date is None
        assert review.author is None
        assert review.title is None

    def test_non_numeric_rating_becomes_none(self, s3_bucket: Any) -> None:
        _put_upload(
            s3_bucket,
            "review,stars\nFine,n/a\n",
            {"text": "review", "rating": "stars"},
            "first_in_file",
        )

        review = extract_upload(_DATASET, _VERSION).reviews[0].review

        assert review.rating is None


# ---------------------------------------------------------------------------
# Empty-text rows (Requirement 3.4)
# ---------------------------------------------------------------------------


class TestSkipsEmptyRows:
    """Rows with empty review text are skipped and counted (Requirement 3.4)."""

    def test_empty_text_rows_skipped_and_counted(self, s3_bucket: Any) -> None:
        _put_upload(
            s3_bucket,
            "review,stars\n"
            "Good,5\n"
            ",4\n"  # empty text
            "   ,3\n"  # whitespace-only text
            "Also good,2\n",
            {"text": "review", "rating": "stars"},
            "first_in_file",
        )

        result = extract_upload(_DATASET, _VERSION)

        assert [r.review.text for r in result.reviews] == ["Good", "Also good"]
        assert result.skipped_empty == 2
        assert result.left_out == 0
        assert result.warnings == []


# ---------------------------------------------------------------------------
# Keep rule under / over the cap (Requirement 3.4, Property 2)
# ---------------------------------------------------------------------------


class TestKeepRule:
    """At most MAX_REVIEWS rows are kept, with a warning when rows drop."""

    def test_under_cap_keeps_all_no_warning(self, s3_bucket: Any) -> None:
        # MAX_REVIEWS is 3; this file has 2 usable rows.
        _put_upload(
            s3_bucket,
            "review\nOne\nTwo\n",
            {"text": "review"},
            "first_in_file",
        )

        result = extract_upload(_DATASET, _VERSION)

        assert len(result.reviews) == 2
        assert result.left_out == 0
        assert result.warnings == []

    def test_at_cap_keeps_all_no_warning(self, s3_bucket: Any) -> None:
        _put_upload(
            s3_bucket,
            "review\nOne\nTwo\nThree\n",
            {"text": "review"},
            "first_in_file",
        )

        result = extract_upload(_DATASET, _VERSION)

        assert len(result.reviews) == 3
        assert result.left_out == 0
        assert result.warnings == []

    def test_over_cap_first_in_file_keeps_first_rows_with_warning(self, s3_bucket: Any) -> None:
        _put_upload(
            s3_bucket,
            "review\nOne\nTwo\nThree\nFour\nFive\n",
            {"text": "review"},
            "first_in_file",
        )

        result = extract_upload(_DATASET, _VERSION)

        # MAX_REVIEWS is 3; first 3 rows in file order are kept.
        assert [r.review.text for r in result.reviews] == ["One", "Two", "Three"]
        assert len(result.reviews) == get_settings().max_reviews
        assert result.left_out == 2
        assert len(result.warnings) == 1
        assert "left out 2" in result.warnings[0]

    def test_over_cap_most_recent_by_date_keeps_newest_with_warning(self, s3_bucket: Any) -> None:
        _put_upload(
            s3_bucket,
            "review,when\n"
            "oldest,2020-01-01\n"
            "newest,2026-01-01\n"
            "middle-a,2023-06-01\n"
            "middle-b,2024-06-01\n"
            "second-oldest,2021-01-01\n",
            {"text": "review", "date": "when"},
            "most_recent_by_date",
        )

        result = extract_upload(_DATASET, _VERSION)

        kept = {r.review.text for r in result.reviews}
        # The 3 most recent by date are kept; the 2 oldest are left out.
        assert kept == {"newest", "middle-b", "middle-a"}
        assert result.left_out == 2
        assert len(result.warnings) == 1

    def test_review_count_never_exceeds_max_reviews(self, s3_bucket: Any) -> None:
        """Property 2 (review cap): stored count never exceeds MAX_REVIEWS."""
        rows = "\n".join(f"r{i}" for i in range(50))
        _put_upload(
            s3_bucket,
            f"review\n{rows}\n",
            {"text": "review"},
            "first_in_file",
        )

        result = extract_upload(_DATASET, _VERSION)

        assert len(result.reviews) <= get_settings().max_reviews


# ---------------------------------------------------------------------------
# Corrupt mapping
# ---------------------------------------------------------------------------


class TestInvalidMapping:
    """A mapping.json without a text column is a hard error (corruption)."""

    def test_missing_text_mapping_raises(self, s3_bucket: Any) -> None:
        _put_upload(
            s3_bucket,
            "review\nGood\n",
            {"rating": "review"},  # no text key
            "first_in_file",
        )

        with pytest.raises(ValueError, match="review-text column"):
            extract_upload(_DATASET, _VERSION)


# ---------------------------------------------------------------------------
# TSV support (delimiter sniffing reused from the parser)
# ---------------------------------------------------------------------------


def test_tsv_delimiter_is_sniffed(s3_bucket: Any) -> None:
    _put_upload(
        s3_bucket,
        "review\trating\nGreat, really\t5\n",
        {"text": "review", "rating": "rating"},
        "first_in_file",
    )

    review = extract_upload(_DATASET, _VERSION).reviews[0].review

    # The comma inside the text must not split the cell — tab is the delimiter.
    assert review.text == "Great, really"
    assert review.rating == 5.0


def test_module_exposes_sentinel() -> None:
    """The upload source-page sentinel is 0 (no real page is ever 0)."""
    assert upload_extraction.UPLOAD_SOURCE_PAGE == 0
