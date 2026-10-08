"""Unit tests for app.ingestion.check_session.

Covers the Check Session store primitives against a moto-mocked DynamoDB table
whose schema MATCHES the provisioned `check-sessions` table in platform-
foundation (`infra/lib/data-stack.ts`): a COMPOSITE primary key of partition
key `check_id` (S) + sort key `item_id` (S), on-demand billing, and TTL
attribute `ttl`. Testing against the real schema is the point — a single-key
table would let put_item succeed where the real table rejects it.

- put_session writes a session (header + item rows) and get_session reads it back
- each check item is its OWN row; query returns the `#session` header + N item rows
- the TTL is set ~24 h ahead in epoch seconds, on every row
- initial per-item states (pending / invalid / duplicate_in_batch) round-trip
- capture_prefix is filled from storage.keys when omitted
- claim_item succeeds once (pending → checking) then fails on a second attempt
  (duplicate / concurrent delivery) and can re-claim an item left in `error`
- set_item_fields updates a single item row's field, honours expected_state,
  and rejects unknown field names
- get_session returns None for a missing or expired session
"""

from __future__ import annotations

import time
from collections.abc import Generator

import boto3
import pytest
from app.core.config import get_settings
from app.ingestion import check_session
from app.ingestion.check_session import SESSION_HEADER_ID, CheckItem
from app.storage import keys
from moto import mock_aws

from tests.support.dynamodb import ensure_check_sessions_table

_TABLE = "check-sessions"
_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal AWS env so boto3 won't look up real credentials."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings() -> Generator[None, None, None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _create_table() -> None:
    """Create the check-sessions table with the REAL provisioned schema.

    Mirrors `infra/lib/data-stack.ts`: composite key check_id (HASH) + item_id
    (RANGE), PAY_PER_REQUEST billing, TTL attribute `ttl`. Idempotent via the
    shared helper, so it is safe to call regardless of test order.
    """
    ensure_check_sessions_table(_TABLE, region=_REGION)


def _items() -> list[CheckItem]:
    """A representative batch: one pending, one invalid, one duplicate."""
    return [
        CheckItem(
            item_id="u1",
            input="https://a.example/reviews",
            state="pending",
            normalized="https://a.example/reviews",
        ),
        CheckItem(item_id="u2", input="not a url", state="invalid"),
        CheckItem(
            item_id="u3",
            input="https://a.example/reviews",
            state="duplicate_in_batch",
            normalized="https://a.example/reviews",
        ),
    ]


def _query_rows(check_id: str) -> list[dict[str, object]]:
    """Raw rows under a partition, straight from DynamoDB (for schema asserts)."""
    ddb = boto3.client("dynamodb", region_name=_REGION)
    resp = ddb.query(
        TableName=_TABLE,
        KeyConditionExpression="check_id = :c",
        ExpressionAttributeValues={":c": {"S": check_id}},
    )
    return resp["Items"]


# ---------------------------------------------------------------------------
# put_session / get_session
# ---------------------------------------------------------------------------


class TestPutAndGet:
    @mock_aws
    def test_round_trips_a_session(self) -> None:
        _create_table()
        stored = check_session.put_session("chk-1", "new", _items())

        got = check_session.get_session("chk-1")
        assert got is not None
        assert got.check_id == "chk-1"
        assert got.origin == "new"
        assert got.refresh_dataset_id is None
        assert set(got.items) == {"u1", "u2", "u3"}
        assert got.items["u1"].state == "pending"
        assert got.items["u2"].state == "invalid"
        assert got.items["u3"].state == "duplicate_in_batch"
        assert got.items["u1"].normalized == "https://a.example/reviews"
        # created_at/ttl survive the round trip.
        assert got.created_at == stored.created_at
        assert got.ttl == stored.ttl

    @mock_aws
    def test_each_item_is_its_own_row_plus_a_header_row(self) -> None:
        _create_table()
        check_session.put_session("chk-rows", "new", _items())

        rows = _query_rows("chk-rows")
        item_ids = {row["item_id"]["S"] for row in rows}  # type: ignore[index]
        # One header row (#session) + one row per item — not a single item with
        # a nested map.
        assert item_ids == {SESSION_HEADER_ID, "u1", "u2", "u3"}
        assert len(rows) == 4
        # The item rows carry top-level attributes, not a nested `items` map.
        u1 = next(r for r in rows if r["item_id"]["S"] == "u1")  # type: ignore[index]
        assert "items" not in u1
        assert u1["state"]["S"] == "pending"  # type: ignore[index]
        # The header row is NOT exposed in the public items dict.
        got = check_session.get_session("chk-rows")
        assert got is not None
        assert SESSION_HEADER_ID not in got.items

    @mock_aws
    def test_ttl_is_about_24h_ahead_on_every_row(self) -> None:
        _create_table()
        before = int(time.time())
        stored = check_session.put_session("chk-ttl", "new", _items())
        after = int(time.time())

        assert stored.ttl >= before + check_session.SESSION_TTL_SECONDS
        assert stored.ttl <= after + check_session.SESSION_TTL_SECONDS
        # Sanity: 24 h == 86400 s.
        assert check_session.SESSION_TTL_SECONDS == 86_400

        # Every row (header + items) carries the same ttl so TTL reaps the
        # whole session together.
        rows = _query_rows("chk-ttl")
        ttls = {int(row["ttl"]["N"]) for row in rows}  # type: ignore[index]
        assert ttls == {stored.ttl}

    @mock_aws
    def test_capture_prefix_filled_from_storage_keys(self) -> None:
        _create_table()
        check_session.put_session("chk-pref", "new", _items())
        got = check_session.get_session("chk-pref")
        assert got is not None
        assert got.items["u1"].capture_prefix == keys.check_prefix("chk-pref", "u1")

    @mock_aws
    def test_refresh_origin_carries_dataset_id(self) -> None:
        _create_table()
        check_session.put_session("chk-ref", "refresh", [_items()[0]], refresh_dataset_id="ds-9")
        got = check_session.get_session("chk-ref")
        assert got is not None
        assert got.origin == "refresh"
        assert got.refresh_dataset_id == "ds-9"

    @mock_aws
    def test_duplicate_check_id_is_refused(self) -> None:
        _create_table()
        check_session.put_session("chk-dup", "new", _items())
        with pytest.raises(Exception):  # noqa: B017,PT011 - TransactionCanceled
            check_session.put_session("chk-dup", "new", _items())

    @mock_aws
    def test_get_missing_session_returns_none(self) -> None:
        _create_table()
        assert check_session.get_session("nope") is None

    @mock_aws
    def test_expired_session_treated_as_absent(self) -> None:
        _create_table()
        check_session.put_session("chk-exp", "new", _items())
        # Rewrite the header ttl into the past to simulate a session whose TTL
        # sweep hasn't run yet.
        ddb = boto3.client("dynamodb", region_name=_REGION)
        ddb.update_item(
            TableName=_TABLE,
            Key={"check_id": {"S": "chk-exp"}, "item_id": {"S": SESSION_HEADER_ID}},
            UpdateExpression="SET #ttl = :past",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":past": {"N": str(int(time.time()) - 10)}},
        )
        assert check_session.get_session("chk-exp") is None


# ---------------------------------------------------------------------------
# claim_item
# ---------------------------------------------------------------------------


class TestClaimItem:
    @mock_aws
    def test_claim_succeeds_once_then_fails_on_duplicate_delivery(self) -> None:
        _create_table()
        check_session.put_session("chk-claim", "new", _items())

        # First delivery wins the claim.
        assert check_session.claim_item("chk-claim", "u1") is True
        got = check_session.get_session("chk-claim")
        assert got is not None
        assert got.items["u1"].state == "checking"
        assert got.items["u1"].claimed_at is not None

        # Second (duplicate / concurrent) delivery loses: returns False, no raise.
        assert check_session.claim_item("chk-claim", "u1") is False

    @mock_aws
    def test_can_reclaim_item_in_error_state(self) -> None:
        _create_table()
        check_session.put_session("chk-retry", "new", _items())
        # Simulate a failed attempt that left the item in `error`.
        assert check_session.set_item_fields("chk-retry", "u1", {"state": "error"})
        assert check_session.claim_item("chk-retry", "u1") is True

    @mock_aws
    def test_cannot_claim_invalid_item(self) -> None:
        _create_table()
        check_session.put_session("chk-inv", "new", _items())
        # u2 is `invalid`, not claimable.
        assert check_session.claim_item("chk-inv", "u2") is False

    @mock_aws
    def test_claim_missing_session_returns_false(self) -> None:
        _create_table()
        assert check_session.claim_item("ghost", "u1") is False


# ---------------------------------------------------------------------------
# set_item_fields
# ---------------------------------------------------------------------------


class TestSetItemFields:
    @mock_aws
    def test_updates_a_single_item_field(self) -> None:
        _create_table()
        check_session.put_session("chk-set", "new", _items())

        verdict = {"verdict": "will_work", "reasons": ["24 reviews verified"]}
        ok = check_session.set_item_fields(
            "chk-set",
            "u1",
            {"state": "done", "verdict": verdict, "final_url": "https://a.example/reviews/"},
        )
        assert ok is True

        got = check_session.get_session("chk-set")
        assert got is not None
        assert got.items["u1"].state == "done"
        assert got.items["u1"].verdict == verdict
        assert got.items["u1"].final_url == "https://a.example/reviews/"
        # Other items are untouched.
        assert got.items["u2"].state == "invalid"

    @mock_aws
    def test_expected_state_guard_blocks_stale_write(self) -> None:
        _create_table()
        check_session.put_session("chk-guard", "new", _items())
        # u1 is `pending`; a write expecting `checking` must not apply.
        ok = check_session.set_item_fields(
            "chk-guard", "u1", {"state": "done"}, expected_state="checking"
        )
        assert ok is False
        got = check_session.get_session("chk-guard")
        assert got is not None
        assert got.items["u1"].state == "pending"

    @mock_aws
    def test_expected_state_guard_allows_matching_write(self) -> None:
        _create_table()
        check_session.put_session("chk-guard2", "new", _items())
        assert check_session.claim_item("chk-guard2", "u1") is True
        ok = check_session.set_item_fields(
            "chk-guard2", "u1", {"state": "done"}, expected_state="checking"
        )
        assert ok is True

    @mock_aws
    def test_rejects_unknown_field_name(self) -> None:
        _create_table()
        check_session.put_session("chk-bad", "new", _items())
        with pytest.raises(KeyError):
            check_session.set_item_fields("chk-bad", "u1", {"bogus": 1})

    @mock_aws
    def test_rejects_empty_fields(self) -> None:
        _create_table()
        check_session.put_session("chk-empty", "new", _items())
        with pytest.raises(ValueError):
            check_session.set_item_fields("chk-empty", "u1", {})
