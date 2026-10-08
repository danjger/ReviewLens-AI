"""Property-based tests for app.core.rate_limit.

Property 1: Rate limits hold.
  For any sequence of requests from any mix of client IPs within one window,
  the number allowed per IP SHALL NOT exceed the per-IP limit, and the total
  allowed SHALL NOT exceed the global limit.
  Validates: Requirement 2.4

Property 2: No raw client IPs stored.
  For any request, every rate-limit key, log line, and stored record SHALL
  contain only the hashed IP, never the raw IP.
  Validates: Requirement 2.5
"""

from __future__ import annotations

import ipaddress
import logging
from collections import Counter

import boto3
import pytest
from app.core.config import get_settings
from app.core.errors import RateLimitError
from app.core.rate_limit import check_rate_limit, hash_ip
from hypothesis import given
from hypothesis import strategies as st
from moto import mock_aws

from tests.support.dynamodb import ensure_rate_limit_table

_TABLE = "rate-limits"
_REGION = "us-east-1"
_ACTION = "checks"
# A single fixed window far larger than any test run, so every request in one
# example lands in the same window.  Property 1 is stated "within one window".
_WINDOW = 3600
_NOW = 1_700_000_000.0  # frozen inside the window [1_700_000_000, 1_700_003_600)


# ---------------------------------------------------------------------------
# Environment / table helpers
# ---------------------------------------------------------------------------


def _set_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
    get_settings.cache_clear()


def _create_table() -> None:
    """Create the ``rate-limits`` table (idempotent, shared schema)."""
    ensure_rate_limit_table(_TABLE, region=_REGION)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def _ipv4(draw: st.DrawFn) -> str:
    """Generate a valid IPv4 address string."""
    return str(ipaddress.IPv4Address(draw(st.integers(min_value=0, max_value=2**32 - 1))))


# A short pool of distinct IPs keeps examples fast while still exercising a
# mix of clients hitting the shared global counter.
_ip_pool = st.lists(_ipv4(), min_size=1, max_size=6, unique=True)

# A sequence of requests: each request comes from one of the pool IPs (by
# index) or has no IP at all (None → global-only).
_request_seq = st.lists(
    st.one_of(st.none(), st.integers(min_value=0, max_value=5)),
    min_size=1,
    max_size=40,
)


# ---------------------------------------------------------------------------
# Property 1: Rate limits hold
# ---------------------------------------------------------------------------


@given(ips=_ip_pool, request_indices=_request_seq, limit=st.integers(1, 5))
def test_rate_limits_hold(
    monkeypatch: pytest.MonkeyPatch,
    ips: list[str],
    request_indices: list[int | None],
    limit: int,
) -> None:
    """Property 1: Rate limits hold.

    For any sequence of requests from any mix of client IPs within one window,
    the number allowed per IP SHALL NOT exceed the per-IP limit, and the total
    allowed SHALL NOT exceed the global limit.
    Validates: Requirement 2.4
    """
    with mock_aws():
        _set_aws_env(monkeypatch)
        _create_table()

        allowed_per_ip: Counter[str | None] = Counter()
        total_allowed = 0

        # Freeze time so every request in this example is in the same window.
        monkeypatch.setattr("app.core.rate_limit.time.time", lambda: _NOW)

        for raw_idx in request_indices:
            # Map the drawn index onto a real IP (or None for global-only).
            client_ip: str | None
            if raw_idx is None:
                client_ip = None
            else:
                client_ip = ips[raw_idx % len(ips)]

            try:
                check_rate_limit(_ACTION, client_ip, limit=limit, window_seconds=_WINDOW)
                allowed_per_ip[client_ip] += 1
                total_allowed += 1
            except RateLimitError:
                # Blocked requests do not count as allowed.
                pass

        # Per-IP invariant: no IP is allowed more than *limit* requests.
        for ip, count in allowed_per_ip.items():
            if ip is not None:
                assert count <= limit, f"IP {ip!r} allowed {count} requests, exceeds limit {limit}"

        # Global invariant: the total allowed never exceeds the global limit.
        assert total_allowed <= limit, f"Total allowed {total_allowed} exceeds global limit {limit}"


# ---------------------------------------------------------------------------
# Property 2: No raw client IPs stored
# ---------------------------------------------------------------------------


@given(ips=_ip_pool, request_indices=_request_seq)
def test_no_raw_client_ip_stored(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ips: list[str],
    request_indices: list[int | None],
) -> None:
    """Property 2: No raw client IPs stored.

    For any request, every rate-limit key, log line, and stored record SHALL
    contain only the hashed IP, never the raw IP.
    Validates: Requirement 2.5
    """
    with mock_aws():
        _set_aws_env(monkeypatch)
        _create_table()

        monkeypatch.setattr("app.core.rate_limit.time.time", lambda: _NOW)

        # Use a generous limit so most requests succeed and write items.
        limit = 1000

        with caplog.at_level(logging.DEBUG, logger="app.core.rate_limit"):
            for raw_idx in request_indices:
                client_ip = None if raw_idx is None else ips[raw_idx % len(ips)]
                try:
                    check_rate_limit(_ACTION, client_ip, limit=limit, window_seconds=_WINDOW)
                except RateLimitError:
                    pass

        # Every stored DynamoDB item: no attribute or key may contain a raw IP,
        # and each per-IP key must contain the hash instead.
        ddb = boto3.client("dynamodb", region_name=_REGION)
        items = ddb.scan(TableName=_TABLE)["Items"]

        used_ips = {ips[i % len(ips)] for i in request_indices if i is not None}
        expected_hashes = {hash_ip(ip) for ip in used_ips}

        for item in items:
            serialized = str(item)
            for raw_ip in used_ips:
                assert raw_ip not in serialized, (
                    f"Raw IP {raw_ip!r} found in stored item: {serialized!r}"
                )

        # Every per-IP scope written must key on the hash, not the raw IP.
        ip_pks = {item["PK"]["S"] for item in items if not item["PK"]["S"].endswith("#global")}
        for pk in ip_pks:
            scope = pk.split("#", 1)[1]
            assert scope in expected_hashes, f"Per-IP PK scope {scope!r} is not a known IP hash"

        # Log lines must never contain a raw IP either.
        log_text = "\n".join(record.getMessage() for record in caplog.records)
        for raw_ip in used_ips:
            assert raw_ip not in log_text, f"Raw IP {raw_ip!r} leaked into a log line"
