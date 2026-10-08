"""Unit tests for ``app.ingestion.upload_parser`` (dataset-ingestion task 8.2).

Covers Requirements 7.2, 7.3, and 7.6:

- delimiter sniffing for ``.csv`` and ``.tsv`` (comma vs tab);
- header synonym mapping for each field (text, rating, date, author, title),
  case-insensitively;
- rejection with the staged object deleted when there is no usable text column
  and when the file cannot be parsed (7.3);
- the ``MAX_REVIEWS`` keep rule for an over-limit file, both **with** a date
  column (``most_recent_by_date``) and **without** one (``first_in_file``), with
  ``will_keep`` capped at ``MAX_REVIEWS`` (7.6);
- usable-row counting that ignores blank-text rows.

The pure :func:`parse_bytes` is tested directly (no I/O). The S3-backed
:func:`preview_upload` is tested under moto so the real ``keys.upload_file`` key
and the delete-on-invalid cleanup are exercised against a genuine bucket.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from app.core.config import get_settings
from app.ingestion.upload_parser import (
    KEEP_FIRST,
    KEEP_MOST_RECENT,
    UploadInvalidError,
    keep_rule_for,
    parse_bytes,
    preview_upload,
    suggest_mapping,
)
from app.storage import keys
from app.storage import s3 as s3_mod
from moto import mock_aws

_BUCKET = "reviewlens-test"


# ---------------------------------------------------------------------------
# suggest_mapping — header synonyms (Requirement 7.2)
# ---------------------------------------------------------------------------


def test_suggest_mapping_detects_each_field() -> None:
    mapping = suggest_mapping(["review", "rating", "date", "author", "title"])
    assert mapping == {
        "text": "review",
        "rating": "rating",
        "date": "date",
        "author": "author",
        "title": "title",
    }


def test_suggest_mapping_is_case_insensitive_and_trims() -> None:
    mapping = suggest_mapping(["  Comment ", "STARS", "Review Date"])
    assert mapping["text"] == "  Comment "  # original header preserved
    assert mapping["rating"] == "STARS"
    assert mapping["date"] == "Review Date"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("review", "text"),
        ("text", "text"),
        ("comment", "text"),
        ("body", "text"),
        ("rating", "rating"),
        ("stars", "rating"),
        ("score", "rating"),
        ("date", "date"),
        ("timestamp", "date"),
        ("author", "author"),
        ("reviewer", "author"),
        ("title", "title"),
        ("headline", "title"),
    ],
)
def test_suggest_mapping_each_synonym(header: str, expected: str) -> None:
    assert suggest_mapping([header]).get(expected) == header


def test_suggest_mapping_unknown_headers_are_unmapped() -> None:
    assert suggest_mapping(["foo", "bar"]) == {}


def test_suggest_mapping_each_header_claimed_once() -> None:
    # "name" is an author synonym; "review" is the text column. Each header is
    # used for at most one field.
    mapping = suggest_mapping(["review", "name"])
    assert mapping == {"text": "review", "author": "name"}


# ---------------------------------------------------------------------------
# keep_rule_for — keep rule follows the (confirmed) mapping (Requirement 7.6)
# ---------------------------------------------------------------------------


def test_keep_rule_for_date_mapped_is_most_recent() -> None:
    assert keep_rule_for({"text": "review", "date": "date"}) == KEEP_MOST_RECENT


def test_keep_rule_for_no_date_is_first_in_file() -> None:
    assert keep_rule_for({"text": "review", "rating": "stars"}) == KEEP_FIRST


def test_keep_rule_for_confirmed_date_header_not_auto_detected() -> None:
    """A confirmed date column whose header the synonyms miss still keeps by date.

    Regression for the keep-rule bug (Known Issues A): the synonym table does
    not auto-detect a header named ``when`` as a date (``suggest_mapping`` finds
    no date), but once the analyst confirms it as the date column the keep rule
    must be ``most_recent_by_date`` — the rule follows the *confirmed* mapping,
    not the auto-suggestion.
    """
    # Confirm the premise: the synonym table does NOT auto-detect `when`.
    assert "date" not in suggest_mapping(["review", "when"])
    # But the confirmed mapping maps a date column, so keep-by-date applies.
    assert keep_rule_for({"text": "review", "date": "when"}) == KEEP_MOST_RECENT


# ---------------------------------------------------------------------------
# parse_bytes — delimiter sniffing (Requirement 7.2)
# ---------------------------------------------------------------------------


def test_parse_bytes_sniffs_csv_comma() -> None:
    raw = b"review,rating\nGreat product,5\nTerrible,1\n"
    preview = parse_bytes(raw, max_reviews=1000)
    assert preview.columns == ["review", "rating"]
    assert preview.suggested_mapping == {"text": "review", "rating": "rating"}
    assert preview.usable_rows == 2


def test_parse_bytes_sniffs_tsv_tab() -> None:
    raw = b"review\trating\nGreat, really great\t5\nMeh\t3\n"
    preview = parse_bytes(raw, max_reviews=1000)
    # Tab delimiter means the comma inside the first review is part of the text,
    # not a column break.
    assert preview.columns == ["review", "rating"]
    assert preview.usable_rows == 2
    assert preview.sample_rows[0]["review"] == "Great, really great"


def test_parse_bytes_single_column_csv() -> None:
    raw = b"review\nfirst\nsecond\n"
    preview = parse_bytes(raw, max_reviews=1000)
    assert preview.columns == ["review"]
    assert preview.usable_rows == 2


# ---------------------------------------------------------------------------
# parse_bytes — usable-row counting (Requirement 7.2)
# ---------------------------------------------------------------------------


def test_parse_bytes_counts_only_nonblank_text_rows() -> None:
    raw = (
        b"review,rating\n"
        b"Good,5\n"
        b",3\n"  # blank text -> not usable
        b"   ,2\n"  # whitespace-only text -> not usable
        b"Also good,4\n"
    )
    preview = parse_bytes(raw, max_reviews=1000)
    assert preview.usable_rows == 2


# ---------------------------------------------------------------------------
# parse_bytes — rejections (Requirement 7.3)
# ---------------------------------------------------------------------------


def test_parse_bytes_no_text_column_rejected() -> None:
    raw = b"rating,date\n5,2024-01-01\n"
    with pytest.raises(UploadInvalidError) as exc:
        parse_bytes(raw, max_reviews=1000)
    assert "review text column" in str(exc.value).lower()


def test_parse_bytes_empty_file_rejected() -> None:
    with pytest.raises(UploadInvalidError):
        parse_bytes(b"   \n", max_reviews=1000)


def test_parse_bytes_undecodable_file_rejected() -> None:
    # Invalid UTF-8 byte sequence.
    with pytest.raises(UploadInvalidError):
        parse_bytes(b"\xff\xfe\x00\x01review", max_reviews=1000)


# ---------------------------------------------------------------------------
# parse_bytes — keep rule over the limit (Requirement 7.6)
# ---------------------------------------------------------------------------


def _rows_with_date(n: int) -> bytes:
    lines = ["review,date"]
    for i in range(n):
        lines.append(f"review number {i},2024-01-{(i % 28) + 1:02d}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _rows_without_date(n: int) -> bytes:
    lines = ["review,rating"]
    for i in range(n):
        lines.append(f"review number {i},5")
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_parse_bytes_over_limit_with_date_keeps_most_recent() -> None:
    # 5 usable rows, limit 3 -> keep rule is most_recent_by_date, will_keep=3.
    preview = parse_bytes(_rows_with_date(5), max_reviews=3)
    assert preview.usable_rows == 5
    assert preview.will_keep == 3
    assert preview.keep_rule == KEEP_MOST_RECENT


def test_parse_bytes_over_limit_without_date_keeps_first() -> None:
    preview = parse_bytes(_rows_without_date(5), max_reviews=3)
    assert preview.usable_rows == 5
    assert preview.will_keep == 3
    assert preview.keep_rule == KEEP_FIRST


def test_parse_bytes_under_limit_keeps_all() -> None:
    preview = parse_bytes(_rows_without_date(2), max_reviews=1000)
    assert preview.usable_rows == 2
    assert preview.will_keep == 2
    assert preview.keep_rule == KEEP_FIRST


def test_parse_bytes_under_limit_with_date_still_reports_date_rule() -> None:
    preview = parse_bytes(_rows_with_date(2), max_reviews=1000)
    assert preview.will_keep == 2
    assert preview.keep_rule == KEEP_MOST_RECENT


def test_preview_to_dict_shape() -> None:
    preview = parse_bytes(b"review,rating\nGood,5\n", max_reviews=1000)
    body = preview.to_dict()
    assert set(body.keys()) == {
        "columns",
        "suggested_mapping",
        "sample_rows",
        "usable_rows",
        "will_keep",
        "keep_rule",
    }


# ---------------------------------------------------------------------------
# preview_upload — S3-backed wrapper (Requirements 7.3, 7.6)
# ---------------------------------------------------------------------------


@pytest.fixture()
def s3_bucket(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A moto-backed S3 bucket so ``preview_upload`` reads/deletes real objects."""
    monkeypatch.setenv("S3_BUCKET", _BUCKET)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "")
    monkeypatch.setenv("MAX_UPLOAD_MB", "10")
    monkeypatch.setenv("MAX_REVIEWS", "3")
    get_settings.cache_clear()
    s3_mod.reset_client()
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET)
        yield client
    get_settings.cache_clear()
    s3_mod.reset_client()


def _put(client: Any, upload_id: str, raw: bytes) -> str:
    key = keys.upload_file(upload_id)
    client.put_object(Bucket=_BUCKET, Key=key, Body=raw)
    return key


def test_preview_upload_valid_file(s3_bucket: Any) -> None:
    key = _put(s3_bucket, "u1", b"review,rating\nGood,5\nBad,1\n")
    preview = preview_upload("u1")
    assert preview.usable_rows == 2
    assert preview.suggested_mapping["text"] == "review"
    # Object is untouched for a valid file.
    assert s3_mod.object_exists(key)


def test_preview_upload_over_limit_with_date_deletes_nothing_but_reports_rule(
    s3_bucket: Any,
) -> None:
    # MAX_REVIEWS=3 from the fixture; 5 rows with a date column.
    _put(s3_bucket, "u2", _rows_with_date(5))
    preview = preview_upload("u2")
    assert preview.usable_rows == 5
    assert preview.will_keep == 3
    assert preview.keep_rule == KEEP_MOST_RECENT


def test_preview_upload_over_limit_without_date_first_in_file(s3_bucket: Any) -> None:
    _put(s3_bucket, "u3", _rows_without_date(5))
    preview = preview_upload("u3")
    assert preview.usable_rows == 5
    assert preview.will_keep == 3
    assert preview.keep_rule == KEEP_FIRST


def test_preview_upload_no_text_column_rejects_and_deletes(s3_bucket: Any) -> None:
    key = _put(s3_bucket, "u4", b"rating,date\n5,2024-01-01\n")
    with pytest.raises(UploadInvalidError):
        preview_upload("u4")
    # Invalid upload is deleted (Requirement 7.3).
    assert not s3_mod.object_exists(key)


def test_preview_upload_unparseable_rejects_and_deletes(s3_bucket: Any) -> None:
    key = _put(s3_bucket, "u5", b"\xff\xfe\x00binary-not-text")
    with pytest.raises(UploadInvalidError):
        preview_upload("u5")
    assert not s3_mod.object_exists(key)


def test_preview_upload_over_size_rejects_and_deletes(
    s3_bucket: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set a tiny upload limit so a small valid CSV is "over size".
    monkeypatch.setenv("MAX_UPLOAD_MB", "0")
    get_settings.cache_clear()
    key = _put(s3_bucket, "u6", b"review\nGood\n")
    with pytest.raises(UploadInvalidError) as exc:
        preview_upload("u6")
    assert "upload limit" in str(exc.value).lower()
    assert not s3_mod.object_exists(key)
