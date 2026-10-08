"""Collision-safe DynamoDB table setup for moto-backed unit and property tests.

Background
----------
Many unit and property tests exercise code that reads or writes a DynamoDB
table (the ``rate-limits`` counters, the ``check-sessions`` store, the
``ws-connections`` registry) against an in-memory :func:`moto.mock_aws`
backend. Each test used to carry its own copy-pasted ``_create_table`` helper
that called ``create_table`` assuming a pristine backend.

Two things made that assumption fragile:

1. ``AWS_ENDPOINT_URL`` leaking from the local environment (it points at the
   Compose LocalStack on ``:4566``). moto 5 honours that variable, so a
   ``@mock_aws`` test would silently talk to the *real* LocalStack — whose
   tables already exist and persist — and every ``create_table`` after the
   first collided with ``ResourceInUseException: Table already exists``. The
   autouse fixture in ``tests/conftest.py`` now scrubs that variable for the
   unit/property suites so moto's in-memory backend is actually used.

2. Even against a clean moto backend, two tests in one module that each create
   the same table are safe only if the backend resets between them. Making the
   setup idempotent removes any dependence on reset timing or test order.

This module centralises the table schemas (matching ``infra/lib/data-stack.ts``)
and makes creation idempotent: creating a table that already exists is a no-op
rather than an error. The tables still exist with the correct schema before any
test body runs, so nothing a test asserts is weakened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import boto3
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

_DEFAULT_REGION = "us-east-1"

# Table names match the provisioned infrastructure defaults in
# ``infra/lib/data-stack.ts`` and ``app.core.config.Settings``.
RATE_LIMIT_TABLE = "rate-limits"
CHECK_SESSIONS_TABLE = "check-sessions"
WS_CONNECTIONS_TABLE = "ws-connections"


def _client(region: str) -> DynamoDBClient:
    return cast("DynamoDBClient", boto3.client("dynamodb", region_name=region))


def _table_exists(ddb: DynamoDBClient, name: str) -> bool:
    try:
        ddb.describe_table(TableName=name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise
    return True


def ensure_rate_limit_table(name: str = RATE_LIMIT_TABLE, *, region: str = _DEFAULT_REGION) -> None:
    """Ensure the ``rate-limits`` table exists (PK ``PK``, PAY_PER_REQUEST, TTL ``ttl``).

    Idempotent: a no-op if the table already exists, so it is safe to call from
    every test regardless of order or what a prior test left behind.
    """
    ddb = _client(region)
    if _table_exists(ddb, name):
        return
    ddb.create_table(
        TableName=name,
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.update_time_to_live(
        TableName=name,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
    )


def ensure_check_sessions_table(
    name: str = CHECK_SESSIONS_TABLE, *, region: str = _DEFAULT_REGION
) -> None:
    """Ensure the ``check-sessions`` table exists with the provisioned schema.

    Composite key ``check_id`` (HASH) + ``item_id`` (RANGE), PAY_PER_REQUEST
    billing, TTL attribute ``ttl`` — mirrors ``infra/lib/data-stack.ts``.
    Idempotent.
    """
    ddb = _client(region)
    if _table_exists(ddb, name):
        return
    ddb.create_table(
        TableName=name,
        AttributeDefinitions=[
            {"AttributeName": "check_id", "AttributeType": "S"},
            {"AttributeName": "item_id", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "check_id", "KeyType": "HASH"},
            {"AttributeName": "item_id", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.update_time_to_live(
        TableName=name,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
    )


def ensure_ws_connections_table(
    name: str = WS_CONNECTIONS_TABLE, *, region: str = _DEFAULT_REGION
) -> None:
    """Ensure the ``ws-connections`` table exists (PK ``connection_id``, PAY_PER_REQUEST).

    Mirrors ``infra/lib/data-stack.ts``. Idempotent.
    """
    ddb = _client(region)
    if _table_exists(ddb, name):
        return
    ddb.create_table(
        TableName=name,
        AttributeDefinitions=[{"AttributeName": "connection_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "connection_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
