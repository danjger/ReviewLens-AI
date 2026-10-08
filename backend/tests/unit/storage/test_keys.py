"""Unit tests for app.storage.keys.

Covers:
- Correct format and prefix for every key builder
- Different inputs produce different keys
- Permanent dataset keys always start with ``datasets/{id}/``
- Temporary keys start with ``checks/`` or ``uploads/``
- No two different artifact types produce the same key for the same inputs
"""

from __future__ import annotations

import pytest
from app.storage import keys

DATASET_ID = "d1a2b3c4-0000-0000-0000-000000000001"
CHECK_ID = "c1a2b3c4-0000-0000-0000-000000000002"
ITEM_ID = "item-001"
UPLOAD_ID = "u1a2b3c4-0000-0000-0000-000000000003"
EXCHANGE_ID = "e1a2b3c4-0000-0000-0000-000000000004"
ISO_TS = "2024-01-15T12:34:56Z"
VERSION = 3
PAGE_NUM = 7


# ---------------------------------------------------------------------------
# Check artifact keys
# ---------------------------------------------------------------------------


class TestCheckKeys:
    def test_check_page_format(self) -> None:
        key = keys.check_page(CHECK_ID, ITEM_ID)
        assert key == f"checks/{CHECK_ID}/{ITEM_ID}/page.html"

    def test_check_snapshot_format(self) -> None:
        key = keys.check_snapshot(CHECK_ID, ITEM_ID)
        assert key == f"checks/{CHECK_ID}/{ITEM_ID}/snapshot.png"

    def test_check_plan_format(self) -> None:
        key = keys.check_plan(CHECK_ID, ITEM_ID)
        assert key == f"checks/{CHECK_ID}/{ITEM_ID}/plan.json"

    def test_check_keys_start_with_checks_prefix(self) -> None:
        assert keys.check_page(CHECK_ID, ITEM_ID).startswith("checks/")
        assert keys.check_snapshot(CHECK_ID, ITEM_ID).startswith("checks/")
        assert keys.check_plan(CHECK_ID, ITEM_ID).startswith("checks/")

    def test_check_keys_differ_by_artifact_type(self) -> None:
        page = keys.check_page(CHECK_ID, ITEM_ID)
        snapshot = keys.check_snapshot(CHECK_ID, ITEM_ID)
        plan = keys.check_plan(CHECK_ID, ITEM_ID)
        assert len({page, snapshot, plan}) == 3

    def test_check_keys_differ_by_check_id(self) -> None:
        assert keys.check_page(CHECK_ID, ITEM_ID) != keys.check_page("other-id", ITEM_ID)

    def test_check_keys_differ_by_item_id(self) -> None:
        assert keys.check_page(CHECK_ID, ITEM_ID) != keys.check_page(CHECK_ID, "other-item")


# ---------------------------------------------------------------------------
# Upload key
# ---------------------------------------------------------------------------


class TestUploadKey:
    def test_upload_file_format(self) -> None:
        key = keys.upload_file(UPLOAD_ID)
        assert key == f"uploads/{UPLOAD_ID}/file"

    def test_upload_key_starts_with_uploads_prefix(self) -> None:
        assert keys.upload_file(UPLOAD_ID).startswith("uploads/")

    def test_upload_key_differs_by_upload_id(self) -> None:
        assert keys.upload_file(UPLOAD_ID) != keys.upload_file("other-upload-id")


# ---------------------------------------------------------------------------
# Dataset prefix
# ---------------------------------------------------------------------------


class TestDatasetPrefix:
    def test_prefix_format(self) -> None:
        assert keys.dataset_prefix(DATASET_ID) == f"datasets/{DATASET_ID}/"

    def test_prefix_ends_with_slash(self) -> None:
        assert keys.dataset_prefix(DATASET_ID).endswith("/")

    def test_prefix_differs_by_dataset_id(self) -> None:
        assert keys.dataset_prefix(DATASET_ID) != keys.dataset_prefix("other-id")


# ---------------------------------------------------------------------------
# Permanent dataset artifact keys
# ---------------------------------------------------------------------------


class TestDatasetKeys:
    def test_raw_page_format(self) -> None:
        key = keys.dataset_raw_page(DATASET_ID, VERSION, PAGE_NUM)
        assert key == f"datasets/{DATASET_ID}/raw/v{VERSION}/page-{PAGE_NUM}.html"

    def test_raw_plan_format(self) -> None:
        key = keys.dataset_raw_plan(DATASET_ID, VERSION)
        assert key == f"datasets/{DATASET_ID}/raw/v{VERSION}/plan.json"

    def test_raw_upload_format(self) -> None:
        key = keys.dataset_raw_upload(DATASET_ID, VERSION)
        assert key == f"datasets/{DATASET_ID}/raw/v{VERSION}/upload.csv"

    def test_raw_mapping_format(self) -> None:
        key = keys.dataset_raw_mapping(DATASET_ID, VERSION)
        assert key == f"datasets/{DATASET_ID}/raw/v{VERSION}/mapping.json"

    def test_snapshot_format(self) -> None:
        key = keys.dataset_snapshot(DATASET_ID, VERSION)
        assert key == f"datasets/{DATASET_ID}/snapshot/v{VERSION}.png"

    def test_reviews_format(self) -> None:
        key = keys.dataset_reviews(DATASET_ID, VERSION)
        assert key == f"datasets/{DATASET_ID}/reviews/v{VERSION}.json"

    def test_chat_exchange_format(self) -> None:
        key = keys.dataset_chat_exchange(DATASET_ID, ISO_TS, EXCHANGE_ID)
        assert key == f"datasets/{DATASET_ID}/chat/{ISO_TS}-{EXCHANGE_ID}.json"

    def test_all_dataset_keys_start_with_dataset_prefix(self) -> None:
        prefix = keys.dataset_prefix(DATASET_ID)
        dataset_keys = [
            keys.dataset_raw_page(DATASET_ID, VERSION, PAGE_NUM),
            keys.dataset_raw_plan(DATASET_ID, VERSION),
            keys.dataset_raw_upload(DATASET_ID, VERSION),
            keys.dataset_raw_mapping(DATASET_ID, VERSION),
            keys.dataset_snapshot(DATASET_ID, VERSION),
            keys.dataset_reviews(DATASET_ID, VERSION),
            keys.dataset_chat_exchange(DATASET_ID, ISO_TS, EXCHANGE_ID),
        ]
        for key in dataset_keys:
            assert key.startswith(prefix), f"{key!r} does not start with {prefix!r}"

    def test_raw_page_differs_by_page_num(self) -> None:
        assert keys.dataset_raw_page(DATASET_ID, VERSION, 1) != keys.dataset_raw_page(
            DATASET_ID, VERSION, 2
        )

    def test_raw_page_differs_by_version(self) -> None:
        assert keys.dataset_raw_page(DATASET_ID, 1, PAGE_NUM) != keys.dataset_raw_page(
            DATASET_ID, 2, PAGE_NUM
        )

    def test_dataset_keys_are_all_distinct(self) -> None:
        """No two different artifact types produce the same key."""
        artifact_keys = [
            keys.dataset_raw_page(DATASET_ID, VERSION, PAGE_NUM),
            keys.dataset_raw_plan(DATASET_ID, VERSION),
            keys.dataset_raw_upload(DATASET_ID, VERSION),
            keys.dataset_raw_mapping(DATASET_ID, VERSION),
            keys.dataset_snapshot(DATASET_ID, VERSION),
            keys.dataset_reviews(DATASET_ID, VERSION),
            keys.dataset_chat_exchange(DATASET_ID, ISO_TS, EXCHANGE_ID),
        ]
        assert len(artifact_keys) == len(set(artifact_keys)), (
            "Two different artifact builders produced the same key"
        )

    def test_keys_differ_by_dataset_id(self) -> None:
        other = "ffffffff-0000-0000-0000-000000000000"
        assert keys.dataset_reviews(DATASET_ID, VERSION) != keys.dataset_reviews(other, VERSION)

    def test_chat_exchange_differs_by_exchange_id(self) -> None:
        assert keys.dataset_chat_exchange(
            DATASET_ID, ISO_TS, EXCHANGE_ID
        ) != keys.dataset_chat_exchange(DATASET_ID, ISO_TS, "different-uuid")

    def test_chat_exchange_differs_by_iso_ts(self) -> None:
        assert keys.dataset_chat_exchange(
            DATASET_ID, ISO_TS, EXCHANGE_ID
        ) != keys.dataset_chat_exchange(DATASET_ID, "2025-06-01T00:00:00Z", EXCHANGE_ID)


# ---------------------------------------------------------------------------
# Cross-prefix isolation
# ---------------------------------------------------------------------------


class TestCrossPrefixIsolation:
    """Temporary keys must never share a prefix with permanent dataset keys."""

    def test_check_keys_not_under_datasets_prefix(self) -> None:
        for key in [
            keys.check_page(CHECK_ID, ITEM_ID),
            keys.check_snapshot(CHECK_ID, ITEM_ID),
            keys.check_plan(CHECK_ID, ITEM_ID),
        ]:
            assert not key.startswith("datasets/"), (
                f"Check key {key!r} must not start with 'datasets/'"
            )

    def test_upload_key_not_under_datasets_prefix(self) -> None:
        assert not keys.upload_file(UPLOAD_ID).startswith("datasets/")

    @pytest.mark.parametrize(
        "dataset_key",
        [
            keys.dataset_raw_page(DATASET_ID, VERSION, PAGE_NUM),
            keys.dataset_raw_plan(DATASET_ID, VERSION),
            keys.dataset_raw_upload(DATASET_ID, VERSION),
            keys.dataset_raw_mapping(DATASET_ID, VERSION),
            keys.dataset_snapshot(DATASET_ID, VERSION),
            keys.dataset_reviews(DATASET_ID, VERSION),
            keys.dataset_chat_exchange(DATASET_ID, ISO_TS, EXCHANGE_ID),
        ],
    )
    def test_dataset_keys_not_under_checks_or_uploads(self, dataset_key: str) -> None:
        assert not dataset_key.startswith("checks/"), (
            f"Dataset key {dataset_key!r} must not start with 'checks/'"
        )
        assert not dataset_key.startswith("uploads/"), (
            f"Dataset key {dataset_key!r} must not start with 'uploads/'"
        )
