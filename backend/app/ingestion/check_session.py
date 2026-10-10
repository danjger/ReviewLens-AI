"""Check Session store backed by DynamoDB (``check-sessions`` table, TTL 24 h).

A **Check Session** is the short-lived record of one Check run. It holds every
submitted URL's state, verdict, evidence, and capture location until the URLs
are added or the session expires. The table name comes from
``Settings.check_sessions_table``; the table itself is provisioned by
``platform-foundation``.

Per-row data model
------------------
The provisioned table (``infra/lib/data-stack.ts`` in ``platform-foundation``)
has a **composite primary key** — partition key ``check_id`` plus sort key
``item_id`` — with an epoch-seconds TTL attribute ``ttl`` ("one item per check
item"). This module stores one DynamoDB **row per check item** under the shared
``check_id`` partition, matching that table exactly::

    { check_id, item_id, input, normalized, final_url, state, claimed_at,
      hops[], verdict{}, existing_dataset{}, capture_prefix, ttl }

Check-level metadata (``created_at``, ``origin``, ``refresh_dataset_id``) is not
per-item, so it lives on a single **session header row** with the sentinel sort
key :data:`SESSION_HEADER_ID` (``"#session"``)::

    { check_id, item_id: "#session", created_at, ttl, origin, refresh_dataset_id }

Every row — the header and each item — carries the same ``ttl`` so DynamoDB's
TTL sweep reaps the whole session together 24 h after creation (Requirement
4.4). The header row is kept out of the public ``items`` dict.

Per-item updates target a **single row** (``Key={check_id, item_id}``) with a
plain conditional expression on top-level attributes. There are no nested-map
path expressions: claiming one item or writing one verdict is a clean
single-row conditional ``UpdateItem``, which still gives true per-item
atomicity (the real goal of the earlier "items as a map" design).

This module provides the storage primitives that later tasks build on:

- :func:`put_session` — write a new session (header + one row per item) in a
  single atomic ``TransactWriteItems`` with their initial state.
- :func:`get_session` — read a session back (``None`` if expired/absent).
- :func:`claim_item` — conditional ``pending``/``error`` → ``checking`` claim
  that returns a boolean so the handler can drop a duplicate delivery.
- :func:`set_item_fields` — generic per-item field setter (used to write the
  verdict, final URL, hops, existing-dataset match, and the next state).

The DynamoDB client is created the same way as :mod:`app.core.rate_limit`
(boto3 ``dynamodb`` client honouring ``aws_endpoint_url`` for LocalStack),
created via a small factory. The module keeps only a client handle, so it stays
stateless: no correctness depends on process memory.

Privacy: nothing here stores a raw client IP. Rows carry URLs and verdicts
only.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

import boto3
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.storage import keys

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import (
        AttributeValueTypeDef,
        TransactWriteItemTypeDef,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The lifecycle states a single check item moves through.
#:
#: - ``pending`` — queued, not yet claimed by a worker.
#: - ``checking`` — claimed by a worker and in progress.
#: - ``done`` — the check finished and a verdict was written.
#: - ``invalid`` — the submitted line was not a well-formed URL.
#: - ``duplicate_in_batch`` — a repeat of another item in the same submission.
#: - ``error`` — the handler failed on its final attempt; retryable.
#: - ``awaiting_confirmation`` — a ``refresh``-origin item that needs the
#:   analyst to confirm before refreshing (Requirement 6.9).
#: - ``applied`` — Add has created/refreshed a dataset for this item.
ItemState = Literal[
    "pending",
    "checking",
    "done",
    "invalid",
    "duplicate_in_batch",
    "error",
    "awaiting_confirmation",
    "applied",
]

#: Session origin: a brand new Check, or one started by the Library's Refresh.
Origin = Literal["new", "refresh"]

#: Session time-to-live: 24 hours after creation (Requirement 4.4).
SESSION_TTL_SECONDS = 24 * 60 * 60

#: Sort-key sentinel for the per-session header row. Chosen so it sorts ahead of
#: real item IDs and can never collide with one (real item IDs are URL slugs
#: such as ``u1``). Kept out of the public :attr:`CheckSession.items` dict.
SESSION_HEADER_ID = "#session"

#: States from which a worker may claim an item for processing. ``pending`` is
#: the normal path; ``error`` lets a retried item be re-claimed.
_CLAIMABLE_STATES: tuple[ItemState, ...] = ("pending", "error")


# ---------------------------------------------------------------------------
# Typed models
# ---------------------------------------------------------------------------


@dataclass
class CheckItem:
    """One submitted URL — or uploaded saved page — within a Check Session.

    Stored as one DynamoDB row. A **URL item** carries ``input`` (the submitted
    URL) and leaves ``upload_id``/``source_url`` null. An **HTML-upload item**
    (dataset-ingestion task 19 / Requirement 8) instead sets ``upload_id`` to
    the staged ``uploads/{upload_id}/file`` object and leaves ``input`` showing
    the uploaded file name; the handler reads ``upload_id`` to take the upload
    path (no probe/redirect/robots). ``source_url`` is the analyst's optional
    original page URL, stored for display, provenance, and duplicate matching
    only and never fetched; ``normalized``/``final_url`` are populated from it
    only when it was supplied (design DynamoDB ``check-sessions`` data model).
    """

    item_id: str
    input: str
    state: ItemState
    normalized: str | None = None
    final_url: str | None = None
    claimed_at: str | None = None
    hops: list[dict[str, Any]] = field(default_factory=list)
    verdict: dict[str, Any] | None = None
    existing_dataset: dict[str, Any] | None = None
    capture_prefix: str | None = None
    upload_id: str | None = None
    source_url: str | None = None


@dataclass
class CheckSession:
    """A whole Check run: its metadata and its items keyed by ``item_id``."""

    check_id: str
    created_at: str
    ttl: int
    origin: Origin
    items: dict[str, CheckItem]
    refresh_dataset_id: str | None = None


# ---------------------------------------------------------------------------
# DynamoDB client factory
# ---------------------------------------------------------------------------


def _get_dynamodb_client() -> DynamoDBClient:
    """Return a DynamoDB client, honouring ``aws_endpoint_url`` for LocalStack."""
    settings = get_settings()
    endpoint_url = settings.aws_endpoint_url or None
    client: DynamoDBClient = boto3.client("dynamodb", endpoint_url=endpoint_url)
    return client


def _table_name() -> str:
    return get_settings().check_sessions_table


# ---------------------------------------------------------------------------
# (De)serialization to the DynamoDB attribute shape
# ---------------------------------------------------------------------------


def _now_epoch() -> int:
    return int(time.time())


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _to_attr(value: Any) -> AttributeValueTypeDef:  # noqa: ANN401 - arbitrary JSON
    """Serialize a JSON-ish Python value into a DynamoDB attribute value.

    Supports the subset the Check Session needs: strings, bools, ints/floats,
    ``None``, lists, and dicts. Numbers are stored as DynamoDB ``N`` strings.
    """
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, list):
        return {"L": [_to_attr(v) for v in value]}
    if isinstance(value, dict):
        return {"M": {k: _to_attr(v) for k, v in value.items()}}
    raise TypeError(f"cannot serialize value of type {type(value)!r} to DynamoDB")


def _from_attr(attr: Any) -> Any:  # noqa: ANN401 - arbitrary JSON
    """Inverse of :func:`_to_attr`: a DynamoDB attribute value back to Python."""
    if "NULL" in attr:
        return None
    if "BOOL" in attr:
        return bool(attr["BOOL"])
    if "S" in attr:
        return attr["S"]
    if "N" in attr:
        number = attr["N"]
        return int(number) if "." not in number and "e" not in number.lower() else float(number)
    if "L" in attr:
        return [_from_attr(v) for v in attr["L"]]
    if "M" in attr:
        return {k: _from_attr(v) for k, v in attr["M"].items()}
    raise TypeError(f"cannot deserialize DynamoDB attribute: {attr!r}")


def _item_to_row(check_id: str, item: CheckItem, ttl: int) -> dict[str, AttributeValueTypeDef]:
    """Serialize one :class:`CheckItem` into a full DynamoDB row.

    The row carries the composite key (``check_id`` + ``item_id``), every item
    attribute as a top-level attribute, and the shared session ``ttl`` so TTL
    reaps it with the rest of the session.
    """
    return {
        "check_id": {"S": check_id},
        "item_id": {"S": item.item_id},
        "input": _to_attr(item.input),
        "state": _to_attr(item.state),
        "normalized": _to_attr(item.normalized),
        "final_url": _to_attr(item.final_url),
        "claimed_at": _to_attr(item.claimed_at),
        "hops": _to_attr(item.hops),
        "verdict": _to_attr(item.verdict),
        "existing_dataset": _to_attr(item.existing_dataset),
        "capture_prefix": _to_attr(item.capture_prefix),
        "upload_id": _to_attr(item.upload_id),
        "source_url": _to_attr(item.source_url),
        "ttl": {"N": str(ttl)},
    }


def _item_from_row(raw: dict[str, Any]) -> CheckItem:
    """Deserialize a DynamoDB item row into a :class:`CheckItem`."""
    return CheckItem(
        item_id=cast("str", _from_attr(raw["item_id"])),
        input=cast("str", _from_attr(raw["input"])),
        state=cast("ItemState", _from_attr(raw["state"])),
        normalized=cast("str | None", _from_attr(raw["normalized"])),
        final_url=cast("str | None", _from_attr(raw["final_url"])),
        claimed_at=cast("str | None", _from_attr(raw["claimed_at"])),
        hops=cast("list[dict[str, Any]]", _from_attr(raw["hops"])) or [],
        verdict=cast("dict[str, Any] | None", _from_attr(raw["verdict"])),
        existing_dataset=cast("dict[str, Any] | None", _from_attr(raw["existing_dataset"])),
        capture_prefix=cast("str | None", _from_attr(raw["capture_prefix"])),
        # ``upload_id``/``source_url`` are absent on rows written before the
        # HTML-upload path landed, so default them to ``None`` rather than
        # requiring the attribute (a URL item leaves both null anyway).
        upload_id=cast("str | None", _from_attr(raw["upload_id"])) if "upload_id" in raw else None,
        source_url=(
            cast("str | None", _from_attr(raw["source_url"])) if "source_url" in raw else None
        ),
    )


def _header_to_row(
    check_id: str,
    created_at: str,
    ttl: int,
    origin: Origin,
    refresh_dataset_id: str | None,
) -> dict[str, AttributeValueTypeDef]:
    """Serialize the per-session header row (sort key :data:`SESSION_HEADER_ID`)."""
    return {
        "check_id": {"S": check_id},
        "item_id": {"S": SESSION_HEADER_ID},
        "created_at": {"S": created_at},
        "ttl": {"N": str(ttl)},
        "origin": {"S": origin},
        "refresh_dataset_id": _to_attr(refresh_dataset_id),
    }


def _assemble_session(rows: list[dict[str, Any]]) -> CheckSession | None:
    """Build a :class:`CheckSession` from the header + item rows of one query.

    Returns ``None`` when the header row is absent (a session that never existed
    or has already been partially swept).
    """
    header: dict[str, Any] | None = None
    items: dict[str, CheckItem] = {}
    for raw in rows:
        item_id = cast("str", _from_attr(raw["item_id"]))
        if item_id == SESSION_HEADER_ID:
            header = raw
            continue
        items[item_id] = _item_from_row(raw)

    if header is None:
        return None

    refresh_attr = header.get("refresh_dataset_id")
    refresh_dataset_id = (
        cast("str | None", _from_attr(refresh_attr)) if refresh_attr is not None else None
    )
    return CheckSession(
        check_id=cast("str", _from_attr(header["check_id"])),
        created_at=cast("str", _from_attr(header["created_at"])),
        ttl=cast("int", _from_attr(header["ttl"])),
        origin=cast("Origin", _from_attr(header["origin"])),
        items=items,
        refresh_dataset_id=refresh_dataset_id,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def put_session(
    check_id: str,
    origin: Origin,
    items: list[CheckItem],
    refresh_dataset_id: str | None = None,
) -> CheckSession:
    """Create and persist a new Check Session.

    ``items`` are the already-parsed submission lines in their initial state
    (``pending`` for a URL to check, ``invalid`` for a malformed line,
    ``duplicate_in_batch`` for a repeat). Each item's ``capture_prefix`` is
    filled in from :mod:`app.storage.keys` when it is missing, so every item
    knows where its S3 artifacts live without the caller building the path.

    The session is written as one **session header row** plus one **row per
    item**, all in a single :meth:`TransactWriteItems` so the session is created
    atomically. Every row gets a ``ttl`` 24 hours in the future (epoch seconds,
    Requirement 4.4). The header write is guarded by
    ``attribute_not_exists(check_id)`` so a duplicate ``check_id`` is refused
    (check IDs are random UUIDs, so a collision means a bug).

    Returns the stored :class:`CheckSession`.
    """
    created_at = _now_iso()
    ttl = _now_epoch() + SESSION_TTL_SECONDS

    for item in items:
        if item.capture_prefix is None:
            item.capture_prefix = keys.check_prefix(check_id, item.item_id)

    table = _table_name()
    transact_items: list[TransactWriteItemTypeDef] = [
        {
            "Put": {
                "TableName": table,
                "Item": _header_to_row(check_id, created_at, ttl, origin, refresh_dataset_id),
                # Refuse to clobber an existing session with the same check_id.
                "ConditionExpression": "attribute_not_exists(check_id)",
            }
        }
    ]
    for item in items:
        transact_items.append(
            {
                "Put": {
                    "TableName": table,
                    "Item": _item_to_row(check_id, item, ttl),
                }
            }
        )

    client = _get_dynamodb_client()
    client.transact_write_items(TransactItems=transact_items)

    return CheckSession(
        check_id=check_id,
        created_at=created_at,
        ttl=ttl,
        origin=origin,
        items={item.item_id: item for item in items},
        refresh_dataset_id=refresh_dataset_id,
    )


def get_session(check_id: str) -> CheckSession | None:
    """Return the Check Session for *check_id*, or ``None`` if absent/expired.

    Reads every row under the ``check_id`` partition with a single ``Query`` and
    assembles the header + item rows into a :class:`CheckSession`.

    DynamoDB's TTL deletes expired rows lazily (it can lag by up to 48 hours),
    so this also treats a session whose ``ttl`` is already in the past as
    absent, matching the API's 404-on-expiry behaviour.
    """
    client = _get_dynamodb_client()
    response = client.query(
        TableName=_table_name(),
        KeyConditionExpression="check_id = :cid",
        ExpressionAttributeValues={":cid": {"S": check_id}},
        ConsistentRead=True,
    )
    rows = cast("list[dict[str, Any]]", response.get("Items", []))
    if not rows:
        return None

    session = _assemble_session(rows)
    if session is None:
        return None
    if session.ttl <= _now_epoch():
        logger.debug("check session %s is past its TTL; treating as expired", check_id)
        return None
    return session


def claim_item(check_id: str, item_id: str) -> bool:
    """Atomically claim one item for processing (``pending``/``error`` → ``checking``).

    Uses a conditional ``UpdateItem`` on the single item **row**
    (``Key={check_id, item_id}``). The claim succeeds only when the row exists
    and is currently in a claimable state (``pending`` or ``error``); it also
    stamps ``claimed_at`` with the current time so a stuck claim can be reasoned
    about later.

    Returns ``True`` when this caller won the claim, ``False`` when the item was
    already claimed (or finished) — the handler drops a duplicate or concurrent
    delivery on ``False`` rather than this raising. A missing session or item
    also yields ``False``.
    """
    client = _get_dynamodb_client()
    claimable: dict[str, AttributeValueTypeDef] = {
        f":s{i}": {"S": state} for i, state in enumerate(_CLAIMABLE_STATES)
    }
    in_list = ", ".join(claimable.keys())

    try:
        client.update_item(
            TableName=_table_name(),
            Key={"check_id": {"S": check_id}, "item_id": {"S": item_id}},
            UpdateExpression="SET #state = :checking, #claimed = :now",
            ConditionExpression=f"attribute_exists(item_id) AND #state IN ({in_list})",
            ExpressionAttributeNames={
                "#state": "state",
                "#claimed": "claimed_at",
            },
            ExpressionAttributeValues={
                ":checking": {"S": "checking"},
                ":now": {"S": _now_iso()},
                **claimable,
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.debug(
                "claim_item: item %s in session %s already claimed/finished; dropping",
                item_id,
                check_id,
            )
            return False
        raise


def set_item_fields(
    check_id: str,
    item_id: str,
    fields: dict[str, Any],
    *,
    expected_state: ItemState | None = None,
) -> bool:
    """Set one or more fields on a single item with a conditional update.

    This is the generic per-item writer the handler and Add use to record the
    verdict, final URL, hops, existing-dataset match, and the next ``state``
    (for example ``checking`` → ``done``, or ``done`` → ``applied``). Every
    field is written as a top-level attribute on the item's own **row**
    (``Key={check_id, item_id}``) so only that item changes.

    ``fields`` keys must be real item attributes; unknown keys are rejected so a
    typo can't silently write a stray attribute. When ``expected_state`` is
    given, the update is guarded by a condition that the item is currently in
    that state, which callers use to make a transition idempotent (for example
    only the worker that holds the ``checking`` claim writes the verdict).

    Returns ``True`` on success, ``False`` when ``expected_state`` was supplied
    and did not match (the item moved on, so the caller should drop its write).
    Raises ``KeyError`` for an unknown field name and ``ValueError`` for an
    empty ``fields`` map.
    """
    if not fields:
        raise ValueError("fields must not be empty")
    unknown = set(fields) - _SETTABLE_FIELDS
    if unknown:
        raise KeyError(f"unknown check item field(s): {sorted(unknown)}")

    client = _get_dynamodb_client()

    names: dict[str, str] = {}
    values: dict[str, AttributeValueTypeDef] = {}
    assignments: list[str] = []
    for i, (name, value) in enumerate(fields.items()):
        names[f"#f{i}"] = name
        values[f":v{i}"] = _to_attr(value)
        assignments.append(f"#f{i} = :v{i}")
    update_expr = "SET " + ", ".join(assignments)

    condition = "attribute_exists(item_id)"
    if expected_state is not None:
        names["#state"] = "state"
        values[":expected"] = {"S": expected_state}
        condition += " AND #state = :expected"

    try:
        client.update_item(
            TableName=_table_name(),
            Key={"check_id": {"S": check_id}, "item_id": {"S": item_id}},
            UpdateExpression=update_expr,
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.debug(
                "set_item_fields: condition failed for item %s in session %s "
                "(expected_state=%s); dropping write",
                item_id,
                check_id,
                expected_state,
            )
            return False
        raise


#: Item attributes that :func:`set_item_fields` is allowed to write. ``item_id``
#: and ``check_id`` are the composite key and never change, so they are
#: intentionally excluded.
_SETTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "state",
        "normalized",
        "final_url",
        "claimed_at",
        "hops",
        "verdict",
        "existing_dataset",
        "capture_prefix",
        "upload_id",
        "source_url",
    }
)
