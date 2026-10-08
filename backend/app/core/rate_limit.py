"""Fixed-window rate limiting backed by DynamoDB.

Each counter lives in the ``rate-limits`` DynamoDB table (name configured by
``Settings.dynamodb_rate_limit_table``) under a partition key of the form::

    {action}#{scope}

where *scope* is either ``global`` or ``sha256(client_ip).hexdigest()``.

The table also carries a ``ttl`` attribute (epoch seconds, end of the current
1-hour window) that DynamoDB's TTL feature uses to delete stale items
automatically, and a ``window_start`` attribute that lets the code detect when
the window has rolled over and the counter should restart from zero.

Usage::

    from fastapi import Request
    from app.core.rate_limit import check_rate_limit, get_client_ip
    from app.core.config import get_settings

    settings = get_settings()

    @app.post("/api/check")
    async def url_check(request: Request) -> ...:
        client_ip = get_client_ip(request)
        check_rate_limit(
            "checks",
            client_ip,
            settings.rl_checks_per_ip_hour,
        )
        ...

``check_rate_limit`` raises :class:`app.core.errors.RateLimitError` (HTTP 429)
when either the per-IP or the global counter exceeds the limit.  The caller
may catch ``RateLimitError.message`` to forward the ``Retry-After`` hint.

Privacy guarantee
-----------------
Raw client IP addresses are **never** written to DynamoDB, logs, or any other
store.  ``hash_ip`` is the only function that receives a raw IP, and it
immediately converts it to a hex SHA-256 digest.  The hash is what gets stored
and logged.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError
from fastapi import Request

from app.core.config import get_settings
from app.core.errors import RateLimitError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IP hashing
# ---------------------------------------------------------------------------


def hash_ip(ip: str) -> str:
    """Return the hex SHA-256 digest of *ip*.

    This is the **only** place in the codebase that receives a raw IP address;
    the hash is what gets stored in DynamoDB and written to log lines.
    """
    return hashlib.sha256(ip.encode()).hexdigest()


# ---------------------------------------------------------------------------
# CloudFront IP extraction
# ---------------------------------------------------------------------------


def get_client_ip(request: Request) -> str | None:
    """Extract the client IP from the CloudFront ``CloudFront-Viewer-Address`` header.

    CloudFront sets this header to ``<ip>:<port>`` for IPv4 and
    ``[<ipv6>]:<port>`` for IPv6.  The port is stripped before returning.

    Returns ``None`` when the header is absent, which typically means the
    request did not come through CloudFront (local development or tests).
    """
    raw = request.headers.get("CloudFront-Viewer-Address")
    if not raw:
        return None

    # IPv6 address is enclosed in brackets: [::1]:12345
    if raw.startswith("["):
        bracket_end = raw.find("]")
        if bracket_end != -1:
            return raw[1:bracket_end]
        return raw  # malformed but return as-is

    # IPv4: 1.2.3.4:12345 — strip the port
    if ":" in raw:
        return raw.rsplit(":", 1)[0]

    return raw


# ---------------------------------------------------------------------------
# DynamoDB client factory
# ---------------------------------------------------------------------------


def _get_dynamodb_client() -> DynamoDBClient:
    """Return a DynamoDB client, honouring ``aws_endpoint_url`` for LocalStack."""
    settings = get_settings()
    endpoint_url = settings.aws_endpoint_url or None
    client: DynamoDBClient = boto3.client("dynamodb", endpoint_url=endpoint_url)
    return client


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------


def _window_start(now: float, window_seconds: int) -> int:
    """Return the epoch-second start of the fixed window containing *now*."""
    return int(now // window_seconds) * window_seconds


def _window_end(now: float, window_seconds: int) -> int:
    """Return the epoch-second end (exclusive) of the fixed window containing *now*."""
    return _window_start(now, window_seconds) + window_seconds


# ---------------------------------------------------------------------------
# Core counter logic
# ---------------------------------------------------------------------------


def _increment_counter(
    table: str,
    pk: str,
    window_seconds: int,
    now: float,
) -> int:
    """Atomically increment the counter for *pk* and return the new value.

    If the stored ``window_start`` is earlier than the current window, the
    item is replaced with a fresh counter starting at 1.  This handles
    window rollover correctly even when the old item has not yet been
    deleted by DynamoDB's TTL (which can lag by up to 48 hours).

    Returns the new counter value after the increment.
    """
    client = _get_dynamodb_client()
    settings = get_settings()
    table_name = settings.dynamodb_rate_limit_table if table == "" else table

    ws = _window_start(now, window_seconds)
    we = _window_end(now, window_seconds)
    retry_after_s = math.ceil(we - now)

    try:
        # Happy path: the item exists and its window_start matches — just ADD.
        response = client.update_item(
            TableName=table_name,
            Key={"PK": {"S": pk}},
            UpdateExpression="ADD #cnt :one SET #ttl = :ttl",
            ConditionExpression="#ws = :ws",
            ExpressionAttributeNames={
                "#cnt": "count",
                "#ttl": "ttl",
                "#ws": "window_start",
            },
            ExpressionAttributeValues={
                ":one": {"N": "1"},
                ":ttl": {"N": str(we)},
                ":ws": {"N": str(ws)},
            },
            ReturnValues="UPDATED_NEW",
        )
        count_str = response["Attributes"]["count"]["N"]
        return int(count_str)

    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]

        if error_code == "ConditionalCheckFailedException":
            # Window has rolled over (or item doesn't exist yet).
            # Reset with count=1 and the new window timestamps.
            try:
                client.put_item(
                    TableName=table_name,
                    Item={
                        "PK": {"S": pk},
                        "count": {"N": "1"},
                        "window_start": {"N": str(ws)},
                        "ttl": {"N": str(we)},
                    },
                    # Only write if the window really is stale (or missing).
                    # Use attribute_not_exists OR window_start < current ws.
                    ConditionExpression=("attribute_not_exists(#ws) OR #ws < :ws"),
                    ExpressionAttributeNames={"#ws": "window_start"},
                    ExpressionAttributeValues={":ws": {"N": str(ws)}},
                )
                return 1

            except ClientError as inner_exc:
                inner_code = inner_exc.response["Error"]["Code"]
                if inner_code == "ConditionalCheckFailedException":
                    # A concurrent request won the race and already reset the
                    # counter.  Retry the ADD now that the item is current.
                    response = client.update_item(
                        TableName=table_name,
                        Key={"PK": {"S": pk}},
                        UpdateExpression="ADD #cnt :one SET #ttl = :ttl",
                        ConditionExpression="#ws = :ws",
                        ExpressionAttributeNames={
                            "#cnt": "count",
                            "#ttl": "ttl",
                            "#ws": "window_start",
                        },
                        ExpressionAttributeValues={
                            ":one": {"N": "1"},
                            ":ttl": {"N": str(we)},
                            ":ws": {"N": str(ws)},
                        },
                        ReturnValues="UPDATED_NEW",
                    )
                    count_str = response["Attributes"]["count"]["N"]
                    return int(count_str)
                raise

        raise

    finally:
        # Suppress the unused variable warning — retry_after_s is referenced
        # only in the calling function.
        _ = retry_after_s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_rate_limit(
    action: str,
    client_ip: str | None,
    limit: int,
    *,
    window_seconds: int = 3600,
) -> None:
    """Check and increment both per-IP and global counters for *action*.

    Raises :class:`~app.core.errors.RateLimitError` with a ``Retry-After``
    message if either counter exceeds *limit* after the increment.

    Parameters
    ----------
    action:
        A short label for what is being rate-limited (e.g. ``"checks"``).
    client_ip:
        The raw client IP address.  When ``None`` (no CloudFront header), only
        the global counter is checked.  The raw IP is hashed immediately and
        never stored.
    limit:
        Maximum number of requests allowed within the window.
    window_seconds:
        Length of the fixed window.  Defaults to one hour (3 600 s).
    """
    now = time.time()
    settings = get_settings()
    table = settings.dynamodb_rate_limit_table

    ws = _window_start(now, window_seconds)
    we = _window_end(now, window_seconds)
    retry_after_s = math.ceil(we - now)

    # Per-IP counter (only when an IP is known).
    if client_ip is not None:
        ip_hash = hash_ip(client_ip)
        ip_pk = f"{action}#{ip_hash}"
        ip_count = _increment_counter(table, ip_pk, window_seconds, now)
        logger.debug(
            "rate_limit ip_check action=%s ip_hash=%s count=%d limit=%d",
            action,
            ip_hash,
            ip_count,
            limit,
        )
        if ip_count > limit:
            raise RateLimitError(
                f"Rate limit exceeded. Retry after {retry_after_s} seconds.",
                code="RATE_LIMIT_EXCEEDED",
                status_code=429,
            )

    # Global counter.
    global_pk = f"{action}#global"
    global_count = _increment_counter(table, global_pk, window_seconds, now)
    logger.debug(
        "rate_limit global_check action=%s count=%d limit=%d",
        action,
        global_count,
        limit,
    )
    if global_count > limit:
        raise RateLimitError(
            f"Rate limit exceeded. Retry after {retry_after_s} seconds.",
            code="RATE_LIMIT_EXCEEDED",
            status_code=429,
        )

    _ = ws  # used implicitly through _window_start / _window_end calls above
