"""Unit tests for app.core.rate_limit.

Covers:
- First request always passes
- Per-IP limit: exactly at limit succeeds, one over fails
- Global limit: shared across different IPs
- Window rollover resets the counter
- IP hashing: raw IP never stored in DynamoDB keys
- get_client_ip: parses CloudFront-Viewer-Address header
- get_client_ip: returns None when header is absent
"""

from __future__ import annotations

import hashlib
from collections.abc import Generator
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import boto3
import pytest
from app.core.config import get_settings
from app.core.errors import RateLimitError
from app.core.rate_limit import check_rate_limit, get_client_ip, hash_ip
from fastapi import Request
from moto import mock_aws

from tests.support.dynamodb import ensure_rate_limit_table

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TABLE = "rate-limits"
_REGION = "us-east-1"
_ACTION = "checks"
_IP = "1.2.3.4"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set minimal AWS env vars so boto3 won't try to look up real credentials."""
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
    """Create the rate-limits DynamoDB table (idempotent, shared schema)."""
    ensure_rate_limit_table(_TABLE, region=_REGION)


# ---------------------------------------------------------------------------
# hash_ip
# ---------------------------------------------------------------------------


class TestHashIp:
    """hash_ip must return the hex SHA-256 digest of the raw IP."""

    def test_returns_hex_sha256(self) -> None:
        expected = hashlib.sha256(_IP.encode()).hexdigest()
        assert hash_ip(_IP) == expected

    def test_different_ips_produce_different_hashes(self) -> None:
        assert hash_ip("1.2.3.4") != hash_ip("1.2.3.5")

    def test_same_ip_is_deterministic(self) -> None:
        assert hash_ip(_IP) == hash_ip(_IP)

    def test_hash_is_64_hex_chars(self) -> None:
        assert len(hash_ip(_IP)) == 64


# ---------------------------------------------------------------------------
# get_client_ip
# ---------------------------------------------------------------------------


class TestGetClientIp:
    """get_client_ip must parse the CloudFront-Viewer-Address header."""

    @staticmethod
    def _request(header_value: str | None) -> Request:
        """Return a minimal Request exposing a ``.headers`` mapping.

        ``get_client_ip`` only calls ``request.headers.get(...)``, so a plain
        dict (which has ``.get``) is enough; the cast keeps mypy happy without
        constructing a full ASGI scope.
        """
        headers: dict[str, str] = {}
        if header_value is not None:
            headers["CloudFront-Viewer-Address"] = header_value
        return cast(Request, SimpleNamespace(headers=headers))

    def test_extracts_ip_from_cloudfront_header(self) -> None:
        """Parses 1.2.3.4:12345 → 1.2.3.4."""
        assert get_client_ip(self._request("1.2.3.4:12345")) == "1.2.3.4"

    def test_extracts_ipv6_from_cloudfront_header(self) -> None:
        """Parses [::1]:12345 → ::1."""
        assert get_client_ip(self._request("[::1]:12345")) == "::1"

    def test_returns_none_when_header_absent(self) -> None:
        """Returns None when the CloudFront header is not present."""
        assert get_client_ip(self._request(None)) is None

    def test_returns_bare_ip_without_port(self) -> None:
        """Header with no port (no colon) is returned as-is."""
        assert get_client_ip(self._request("1.2.3.4")) == "1.2.3.4"


# ---------------------------------------------------------------------------
# check_rate_limit — basic functionality
# ---------------------------------------------------------------------------


class TestCheckRateLimitBasic:
    """Core per-IP and global limit behaviour."""

    @mock_aws
    def test_first_request_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The very first request must never be rejected."""
        monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
        get_settings.cache_clear()
        _create_table()
        # Should not raise
        check_rate_limit(_ACTION, _IP, limit=5)

    @mock_aws
    def test_requests_up_to_limit_are_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exactly *limit* requests must succeed."""
        monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
        get_settings.cache_clear()
        _create_table()
        limit = 3
        for _ in range(limit):
            check_rate_limit(_ACTION, _IP, limit=limit)

    @mock_aws
    def test_limit_reached_raises_rate_limit_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The (limit+1)-th request must raise RateLimitError."""
        monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
        get_settings.cache_clear()
        _create_table()
        limit = 3
        for _ in range(limit):
            check_rate_limit(_ACTION, _IP, limit=limit)
        with pytest.raises(RateLimitError) as exc_info:
            check_rate_limit(_ACTION, _IP, limit=limit)
        assert exc_info.value.status_code == 429
        assert "Retry after" in exc_info.value.message

    @mock_aws
    def test_rate_limit_error_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """RateLimitError must carry the RATE_LIMIT_EXCEEDED code."""
        monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
        get_settings.cache_clear()
        _create_table()
        for _ in range(1):
            check_rate_limit(_ACTION, _IP, limit=1)
        with pytest.raises(RateLimitError) as exc_info:
            check_rate_limit(_ACTION, _IP, limit=1)
        assert exc_info.value.code == "RATE_LIMIT_EXCEEDED"


# ---------------------------------------------------------------------------
# Global limit
# ---------------------------------------------------------------------------


class TestGlobalLimit:
    """Global counter is shared across all IPs."""

    @mock_aws
    def test_global_limit_enforced_across_different_ips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Different IPs share the global counter; the combined total must be capped."""
        monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
        get_settings.cache_clear()
        _create_table()

        limit = 3
        ips = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

        # Each IP makes one request — total = 3, exactly at the limit.
        for ip in ips:
            check_rate_limit(_ACTION, ip, limit=limit)

        # Any further request from any IP should be blocked by the global counter.
        with pytest.raises(RateLimitError):
            check_rate_limit(_ACTION, "10.0.0.4", limit=limit)

    @mock_aws
    def test_no_ip_still_checks_global(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When client_ip is None, the global counter is still incremented and checked."""
        monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
        get_settings.cache_clear()
        _create_table()

        limit = 2
        check_rate_limit(_ACTION, None, limit=limit)
        check_rate_limit(_ACTION, None, limit=limit)
        with pytest.raises(RateLimitError):
            check_rate_limit(_ACTION, None, limit=limit)


# ---------------------------------------------------------------------------
# Window rollover
# ---------------------------------------------------------------------------


class TestWindowRollover:
    """After the window expires a new window starts the counter from zero."""

    @mock_aws
    def test_window_rollover_resets_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the window rolls over, the counter resets and requests are allowed again."""
        monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
        get_settings.cache_clear()
        _create_table()

        limit = 2
        window = 60  # 60-second window for this test

        # Exhaust the limit inside window 1 (t=0).
        t_window1 = 1_700_000_000.0
        with patch("app.core.rate_limit.time.time", return_value=t_window1):
            for _ in range(limit):
                check_rate_limit(_ACTION, _IP, limit=limit, window_seconds=window)
            with pytest.raises(RateLimitError):
                check_rate_limit(_ACTION, _IP, limit=limit, window_seconds=window)

        # Move to the next window (t = window1 + window).
        t_window2 = t_window1 + window
        with patch("app.core.rate_limit.time.time", return_value=t_window2):
            # First request in new window must succeed.
            check_rate_limit(_ACTION, _IP, limit=limit, window_seconds=window)

    @mock_aws
    def test_window_boundary_counts_are_independent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Requests right at the window boundary belong to the new window."""
        monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
        get_settings.cache_clear()
        _create_table()

        limit = 1
        window = 60
        # 1_700_000_040 is an exact window boundary: 1_700_000_040 % 60 == 0
        base = 1_700_000_040.0

        # Use the window before base: t_before is in the prior window.
        t_before = base - 1  # 1_700_000_039, in window starting at 1_699_999_980
        with patch("app.core.rate_limit.time.time", return_value=t_before):
            check_rate_limit(_ACTION, _IP, limit=limit, window_seconds=window)

        # At exactly base (new window starting at 1_700_000_040), both counters reset.
        with patch("app.core.rate_limit.time.time", return_value=base):
            check_rate_limit(_ACTION, _IP, limit=limit, window_seconds=window)


# ---------------------------------------------------------------------------
# Privacy: raw IP never stored
# ---------------------------------------------------------------------------


class TestIpPrivacy:
    """Raw IPs must never appear in DynamoDB keys or stored values."""

    @mock_aws
    def test_ip_hashing_never_stores_raw_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DynamoDB items must not contain the raw IP in any key or attribute."""
        monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
        get_settings.cache_clear()
        _create_table()

        raw_ip = "192.168.99.1"
        check_rate_limit(_ACTION, raw_ip, limit=10)

        # Scan all items in the table and verify none contain the raw IP.
        ddb = boto3.client("dynamodb", region_name=_REGION)
        items = ddb.scan(TableName=_TABLE)["Items"]

        for item in items:
            pk_value = item["PK"]["S"]
            assert raw_ip not in pk_value, f"Raw IP {raw_ip!r} found in DynamoDB PK: {pk_value!r}"
            # Also check no attribute value contains the raw IP.
            for attr_name, attr_val in item.items():
                for val in attr_val.values():
                    assert raw_ip not in str(val), (
                        f"Raw IP {raw_ip!r} found in attribute {attr_name}={attr_val}"
                    )

    @mock_aws
    def test_pk_contains_ip_hash_not_raw_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The PK must contain the SHA-256 hash of the IP, not the raw IP."""
        monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
        get_settings.cache_clear()
        _create_table()

        raw_ip = "203.0.113.42"
        expected_hash = hash_ip(raw_ip)
        check_rate_limit(_ACTION, raw_ip, limit=10)

        ddb = boto3.client("dynamodb", region_name=_REGION)
        items = ddb.scan(TableName=_TABLE)["Items"]

        ip_pk_found = any(expected_hash in item["PK"]["S"] for item in items)
        assert ip_pk_found, f"Expected a PK containing hash {expected_hash!r} but got: " + str(
            [i["PK"]["S"] for i in items]
        )
