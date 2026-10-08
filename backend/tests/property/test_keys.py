"""Property-based tests for app.storage.keys.

Property 6: Keys stay in their prefix.
  For any dataset ID, version, and page number, every permanent key SHALL
  start with ``datasets/{id}/``, and different artifacts SHALL never produce
  the same key.
  Validates: Requirement 5.1
"""

from __future__ import annotations

from app.storage import keys
from hypothesis import given
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# UUIDs and slug-like strings are the normal inputs, but the builders must
# handle any non-empty string without breaking the prefix invariant.
non_empty_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=1,
    max_size=64,
)

positive_int = st.integers(min_value=1, max_value=10_000)

# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------


@given(
    dataset_id=non_empty_text,
    version=positive_int,
    page_num=positive_int,
    iso_ts=non_empty_text,
    exchange_id=non_empty_text,
)
def test_permanent_keys_stay_under_dataset_prefix(
    dataset_id: str,
    version: int,
    page_num: int,
    iso_ts: str,
    exchange_id: str,
) -> None:
    """Property 6: Keys stay in their prefix.

    For any dataset ID, version, and page number, every permanent key SHALL
    start with ``datasets/{id}/``.
    Validates: Requirement 5.1
    """
    prefix = keys.dataset_prefix(dataset_id)
    permanent_keys = [
        keys.dataset_raw_page(dataset_id, version, page_num),
        keys.dataset_raw_plan(dataset_id, version),
        keys.dataset_raw_upload(dataset_id, version),
        keys.dataset_raw_mapping(dataset_id, version),
        keys.dataset_snapshot(dataset_id, version),
        keys.dataset_reviews(dataset_id, version),
        keys.dataset_chat_exchange(dataset_id, iso_ts, exchange_id),
    ]
    for key in permanent_keys:
        assert key.startswith(prefix), f"Key {key!r} does not start with prefix {prefix!r}"


@given(
    dataset_id=non_empty_text,
    version=positive_int,
    page_num=positive_int,
    iso_ts=non_empty_text,
    exchange_id=non_empty_text,
)
def test_different_artifacts_never_produce_the_same_key(
    dataset_id: str,
    version: int,
    page_num: int,
    iso_ts: str,
    exchange_id: str,
) -> None:
    """Property 6: Keys stay in their prefix.

    Different artifact types SHALL never produce the same key for the same
    inputs.
    Validates: Requirement 5.1
    """
    artifact_keys = [
        keys.dataset_raw_page(dataset_id, version, page_num),
        keys.dataset_raw_plan(dataset_id, version),
        keys.dataset_raw_upload(dataset_id, version),
        keys.dataset_raw_mapping(dataset_id, version),
        keys.dataset_snapshot(dataset_id, version),
        keys.dataset_reviews(dataset_id, version),
        keys.dataset_chat_exchange(dataset_id, iso_ts, exchange_id),
    ]
    assert len(artifact_keys) == len(set(artifact_keys)), (
        "Two different artifact builders produced the same key: " + str(sorted(artifact_keys))
    )
