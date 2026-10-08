"""Property-based tests for the review-analysis extraction stage (task 3.4).

Property 1: Dedupe is idempotent and complete.
  For any list of reviews, deduping twice SHALL equal deduping once, and the
  result SHALL contain no two reviews with the same normalized text, author,
  and date.
  Validates: Requirement 3.3

Property 2: Review cap.
  For any upload and any page sequence, the stored review count SHALL never
  exceed MAX_REVIEWS.
  Validates: Requirements 2.1, 3.4

Both properties are tested against the real pipeline stages:

* Property 1 drives ``app.handlers.dedupe.dedupe`` over Hypothesis-generated
  lists of :class:`~app.handlers.extraction_stage.CollectedReview`, varying
  text/author/date including ``None``, surrounding and interior whitespace,
  letter case, and Unicode compatibility variants (NFKC folds these together),
  so the generators exercise exactly the normalization the dedupe key relies on
  and routinely produce collisions. The test re-derives the normalized key with
  an independent copy of the normalization recipe so the oracle can't share a
  bug with the implementation.

* Property 2 drives ``app.handlers.upload_extraction.extract_upload`` over
  Hypothesis-generated CSV files read back from a moto-backed S3 bucket (the
  real ``keys.*`` keys and S3 read path), with ``MAX_REVIEWS`` itself varied,
  and asserts the kept count never exceeds the cap for both keep rules. The
  page-sequence side of the property is enforced by the same keep-rule function
  (and by collection's ``MAX_REVIEWS`` break), so it is also exercised directly
  against ``_apply_keep_rule`` over arbitrary review lists, which is where the
  cap is applied and avoids driving the heavy browser/collection stage.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

import boto3
import pytest
from app.core.config import get_settings
from app.extraction.models import VerifiedReview
from app.handlers.dedupe import dedupe
from app.handlers.extraction_stage import CollectedReview
from app.handlers.upload_extraction import (
    _apply_keep_rule,
    extract_upload,
)
from app.ingestion.upload_parser import KEEP_FIRST, KEEP_MOST_RECENT
from app.storage import keys
from app.storage import s3 as s3_mod
from hypothesis import given
from hypothesis import strategies as st
from moto import mock_aws

# ---------------------------------------------------------------------------
# Shared generators for Property 1
# ---------------------------------------------------------------------------

# A pool of field values chosen to collide under the dedupe normalization
# (NFKC + collapse whitespace + strip + lowercase, None -> ""). Each cluster
# normalizes to the same key fragment, so lists built from this pool routinely
# contain duplicates that dedupe must collapse:
#   - case variants: "Great" / "great" / "GREAT"
#   - whitespace variants: "  good  " / "good" / "good\tproduct"
#   - Unicode compatibility variants: "ﬁ" (U+FB01 ligature) -> "fi", full-width
#     digits -> ASCII, non-breaking space -> ordinary space
#   - None vs "" (a missing optional field normalizes to "")
_FIELD_VALUES: tuple[str | None, ...] = (
    None,
    "",
    "   ",
    "Great",
    "great",
    "GREAT",
    "  great  ",
    "good product",
    "good   product",
    "good\tproduct",
    "ﬁne",  # U+FB01 ligature -> "fine" under NFKC
    "fine",
    "ＡＢＣ",  # full-width -> "abc"
    "abc",
    "caf\u00e9",  # composed é
    "cafe\u0301",  # decomposed e + combining acute -> same NFKC form
    "J.\u00a0D.",  # non-breaking space -> ordinary space
    "J. D.",
    "2026-07-01",
    "2026-07-02",
)

_FIELD_STRATEGY = st.sampled_from(_FIELD_VALUES)

# Also mix in free-form text so the generators aren't limited to the collision
# pool; this covers arbitrary Unicode review bodies too.
_FREE_TEXT = st.text(max_size=12)


def _collected_review() -> st.SearchStrategy[CollectedReview]:
    """A ``CollectedReview`` with varied text/author/date and a source page.

    ``text`` is required by :class:`VerifiedReview`, so it is drawn from a
    non-``None`` mix of the collision pool and free text; ``author`` and
    ``date`` include ``None`` to exercise the missing-field branch of the key.
    """
    text = st.one_of(
        st.sampled_from([v for v in _FIELD_VALUES if v is not None]),
        _FREE_TEXT,
    )
    optional = st.one_of(_FIELD_STRATEGY, _FREE_TEXT, st.none())
    return st.builds(
        lambda t, author, date, page: CollectedReview(
            review=VerifiedReview(text=t, author=author, date=date),
            source_page=page,
        ),
        text,
        optional,
        optional,
        st.integers(min_value=0, max_value=10),
    )


def _normalize_field(value: str | None) -> str:
    """Independent copy of the dedupe normalization (oracle, not imported).

    Mirrors ``app.handlers.dedupe._normalize_field`` by the recipe in the design
    (NFKC, collapse whitespace, strip, lowercase, ``None`` -> ""), reimplemented
    here so the test's expected key cannot share a bug with the implementation.
    """
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).strip().lower()


def _key(collected: CollectedReview) -> tuple[str, str, str]:
    """The normalized (text, author, date) key for a review, via the oracle."""
    review = collected.review
    return (
        _normalize_field(review.text),
        _normalize_field(review.author),
        _normalize_field(review.date),
    )


# ---------------------------------------------------------------------------
# Property 1: Dedupe is idempotent and complete (Requirement 3.3)
# ---------------------------------------------------------------------------


@given(reviews=st.lists(_collected_review(), max_size=40))
def test_dedupe_is_idempotent(reviews: list[CollectedReview]) -> None:
    """Property 1: Dedupe is idempotent and complete.

    Deduping twice equals deduping once, for any list of reviews.
    Validates: Requirement 3.3
    """
    once = dedupe(reviews)
    twice = dedupe(once)
    assert twice == once


@given(reviews=st.lists(_collected_review(), max_size=40))
def test_dedupe_result_has_no_duplicate_keys(reviews: list[CollectedReview]) -> None:
    """Property 1: Dedupe is idempotent and complete.

    The result contains no two reviews with the same normalized text, author,
    and date.
    Validates: Requirement 3.3
    """
    result = dedupe(reviews)
    seen_keys = [_key(collected) for collected in result]
    assert len(seen_keys) == len(set(seen_keys))


@given(reviews=st.lists(_collected_review(), max_size=40))
def test_dedupe_keeps_first_occurrence_in_order(reviews: list[CollectedReview]) -> None:
    """Property 1: Dedupe is idempotent and complete.

    The result keeps the first occurrence of each key and preserves order, so a
    deduped list is exactly the input filtered to first-seen keys. This pins
    down *which* representative survives, strengthening completeness.
    Validates: Requirement 3.3
    """
    result = dedupe(reviews)

    expected: list[CollectedReview] = []
    seen: set[tuple[str, str, str]] = set()
    for collected in reviews:
        key = _key(collected)
        if key not in seen:
            seen.add(key)
            expected.append(collected)

    assert result == expected


# ---------------------------------------------------------------------------
# Property 2: Review cap — upload path via extract_upload (Requirements 2.1, 3.4)
# ---------------------------------------------------------------------------

_BUCKET = "reviewlens-test"
_DATASET = "ds-prop-cap"
_VERSION = 1


@pytest.fixture()
def s3_bucket(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A moto-backed S3 bucket so ``extract_upload`` reads real objects.

    ``MAX_REVIEWS`` is set per example by :func:`_put_upload` via the env; the
    bucket itself is created once per generated example here. Hypothesis reuses
    this function-scoped fixture across examples (the ``dev``/``ci`` profiles
    suppress the function-scoped-fixture health check), and each example writes
    its own objects to the same keys, overwriting the previous one.
    """
    monkeypatch.setenv("S3_BUCKET", _BUCKET)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "")
    get_settings.cache_clear()
    s3_mod.reset_client()
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET)
        yield client
    get_settings.cache_clear()
    s3_mod.reset_client()


def _put_upload(client: Any, csv_text: str, keep_rule: str, max_reviews: int) -> None:
    """Write the CSV + mapping.json and pin ``MAX_REVIEWS`` for this example."""
    import os

    os.environ["MAX_REVIEWS"] = str(max_reviews)
    get_settings.cache_clear()
    s3_mod.reset_client()
    mapping = {"text": "review"}
    if keep_rule == KEEP_MOST_RECENT:
        mapping["date"] = "when"
    client.put_object(
        Bucket=_BUCKET,
        Key=keys.dataset_raw_upload(_DATASET, _VERSION),
        Body=csv_text.encode("utf-8"),
    )
    doc = {"mapping": mapping, "keep_rule": keep_rule}
    client.put_object(
        Bucket=_BUCKET,
        Key=keys.dataset_raw_mapping(_DATASET, _VERSION),
        Body=json.dumps(doc).encode("utf-8"),
    )


def _csv_rows(texts: list[str], dates: list[str] | None) -> str:
    """Build CSV text with a required ``review`` column and optional ``when``."""
    if dates is None:
        lines = ["review", *texts]
        return "\n".join(lines) + "\n"
    header = "review,when"
    body = [f"{t},{d}" for t, d in zip(texts, dates, strict=True)]
    return "\n".join([header, *body]) + "\n"


# Non-empty, comma/newline-free review text so each generated row is usable and
# the CSV stays well-formed (the keep-rule cap, not CSV parsing, is under test).
# Surrogates are excluded because the row is UTF-8 encoded into the CSV body;
# a lone surrogate can't be encoded and is a serialization artifact of the test
# harness, not of the keep-rule cap under test.
# The double-quote is excluded too: in CSV it starts a quoted field, so a cell
# containing a bare ``"`` would merge or split rows and break the 1-text ==
# 1-row correspondence the exact-count oracle relies on. That is a property of
# CSV quoting, not of the keep-rule cap, so keeping each text to one plain field
# lets the test assert the exact kept count rather than only the ``<= cap``
# bound (which it still asserts).
_ROW_TEXT = st.text(
    alphabet=st.characters(
        blacklist_characters=',"\n\r\t',
        min_codepoint=32,
        blacklist_categories=("Cs",),
    ),
    min_size=1,
    max_size=8,
).filter(lambda s: s.strip() != "")


@given(
    texts=st.lists(_ROW_TEXT, min_size=0, max_size=25),
    max_reviews=st.integers(min_value=1, max_value=10),
    keep_rule=st.sampled_from([KEEP_FIRST, KEEP_MOST_RECENT]),
    data=st.data(),
)
def test_upload_review_count_never_exceeds_cap(
    s3_bucket: Any,
    texts: list[str],
    max_reviews: int,
    keep_rule: str,
    data: st.DataObject,
) -> None:
    """Property 2: Review cap.

    For any upload and any ``MAX_REVIEWS``, the stored review count never
    exceeds the cap, for both keep rules.
    Validates: Requirements 2.1, 3.4
    """
    dates: list[str] | None = None
    if keep_rule == KEEP_MOST_RECENT:
        dates = data.draw(
            st.lists(
                st.dates().map(lambda d: d.isoformat()),
                min_size=len(texts),
                max_size=len(texts),
            )
        )

    _put_upload(s3_bucket, _csv_rows(texts, dates), keep_rule, max_reviews)

    result = extract_upload(_DATASET, _VERSION)

    assert len(result.reviews) <= max_reviews
    # And the cap is only ever binding when there were more usable rows than it.
    assert len(result.reviews) == min(len(texts), max_reviews)
    # A warning is recorded exactly when rows were left out (Requirement 3.4).
    assert (result.left_out > 0) == bool(result.warnings)
    assert result.left_out == max(0, len(texts) - max_reviews)


# ---------------------------------------------------------------------------
# Property 2: Review cap — keep-rule function directly (page-sequence side)
# ---------------------------------------------------------------------------


def _verified_review() -> st.SearchStrategy[VerifiedReview]:
    """A ``VerifiedReview`` with non-empty text and an optional ISO date."""
    date = st.one_of(st.none(), st.dates().map(lambda d: d.isoformat()))
    return st.builds(
        lambda text, d: VerifiedReview(text=text, date=d),
        st.text(min_size=1, max_size=8),
        date,
    )


@given(
    reviews=st.lists(_verified_review(), max_size=40),
    max_reviews=st.integers(min_value=1, max_value=15),
    keep_rule=st.sampled_from([KEEP_FIRST, KEEP_MOST_RECENT, "unknown_rule"]),
)
def test_keep_rule_never_exceeds_cap(
    reviews: list[VerifiedReview],
    max_reviews: int,
    keep_rule: str,
) -> None:
    """Property 2: Review cap.

    ``_apply_keep_rule`` — the single place both the upload path and the URL
    page-sequence cap enforce ``MAX_REVIEWS`` — never returns more than the cap,
    and the left-out count accounts for every dropped review, for any keep rule
    (including an unknown rule, which falls back to first-in-file).
    Validates: Requirements 2.1, 3.4
    """
    kept, left_out = _apply_keep_rule(reviews, keep_rule, max_reviews)

    assert len(kept) <= max_reviews
    assert len(kept) == min(len(reviews), max_reviews)
    assert left_out == max(0, len(reviews) - max_reviews)
    assert len(kept) + left_out == len(reviews)
