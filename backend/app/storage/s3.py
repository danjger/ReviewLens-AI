"""S3 object helpers for ReviewLens AI.

A thin, typed wrapper over the boto3 S3 client so that the rest of the
application never builds an S3 client by hand. Object *keys* always come from
:mod:`app.storage.keys`; this module only reads and writes bytes for a key.

The client is memoised per process and honours the ``aws_endpoint_url``
override used by LocalStack in the local stack and integration tests, matching
the pattern already used by :mod:`app.events.publisher` and
:mod:`app.core.rate_limit`.

Nothing here depends on process memory for correctness — the only cached value
is the boto3 client handle, which is a connection pool, not application state.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import boto3

from app.core.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

logger = logging.getLogger(__name__)

_client: S3Client | None = None


def _get_s3_client() -> S3Client:
    """Return a shared S3 client, honouring ``aws_endpoint_url``.

    The endpoint override lets LocalStack receive objects in local and
    integration runs; in AWS the override is empty and the default endpoint is
    used.
    """
    global _client
    if _client is None:
        settings = get_settings()
        endpoint_url = settings.aws_endpoint_url or None
        _client = cast("S3Client", boto3.client("s3", endpoint_url=endpoint_url))
    return _client


def reset_client() -> None:
    """Clear the cached S3 client.

    Intended for tests that enter a moto ``mock_aws`` context or change
    settings between cases. Safe to call when no client has been created.
    """
    global _client
    _client = None


def put_bytes(key: str, body: bytes, *, content_type: str) -> None:
    """Write *body* to the application bucket under *key*.

    Args:
        key: The S3 object key (always built via :mod:`app.storage.keys`).
        body: The raw object bytes.
        content_type: The object's MIME type, e.g. ``text/html`` or
            ``image/png``.

    The target bucket is ``Settings.s3_bucket``.
    """
    client = _get_s3_client()
    bucket = get_settings().s3_bucket
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    logger.debug("Wrote %d bytes to s3://%s/%s", len(body), bucket, key)


def get_bytes(key: str) -> bytes:
    """Read the object stored under *key* from the application bucket.

    Args:
        key: The S3 object key (always built via :mod:`app.storage.keys`).

    Returns:
        The raw object bytes.

    The target bucket is ``Settings.s3_bucket``. Raises the underlying boto3
    ``ClientError`` (for example ``NoSuchKey``) when the object is absent.
    """
    client = _get_s3_client()
    bucket = get_settings().s3_bucket
    response = client.get_object(Bucket=bucket, Key=key)
    body: bytes = response["Body"].read()
    logger.debug("Read %d bytes from s3://%s/%s", len(body), bucket, key)
    return body


def copy_object(source_key: str, dest_key: str) -> None:
    """Copy an object within the application bucket from *source_key* to *dest_key*.

    Both keys always come from :mod:`app.storage.keys`. This is a server-side
    S3 copy (the bytes never travel through the process), used by the Refresh
    Service to promote a Check's temporary capture (page, plan, snapshot) or an
    upload's file and mapping into a dataset's permanent ``raw/v{n}`` location
    for a new data version.

    Args:
        source_key: The existing object's key.
        dest_key: The destination key to write.

    The bucket is ``Settings.s3_bucket`` for both source and destination. Raises
    the underlying boto3 ``ClientError`` (for example ``NoSuchKey``) when the
    source object is absent.
    """
    client = _get_s3_client()
    bucket = get_settings().s3_bucket
    client.copy_object(
        Bucket=bucket,
        Key=dest_key,
        CopySource={"Bucket": bucket, "Key": source_key},
    )
    logger.debug("Copied s3://%s/%s -> s3://%s/%s", bucket, source_key, bucket, dest_key)


def object_exists(key: str) -> bool:
    """Return True when an object exists under *key* in the application bucket.

    Used by the Refresh Service to copy optional artifacts (a snapshot, an
    upload mapping) only when they are present, without raising on absence.

    Args:
        key: The S3 object key (always built via :mod:`app.storage.keys`).

    Returns:
        True if the object exists, False if it does not.
    """
    from botocore.exceptions import ClientError

    client = _get_s3_client()
    bucket = get_settings().s3_bucket
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return True


def delete_object(key: str) -> None:
    """Delete the object stored under *key* from the application bucket.

    Used to clean up partial permanent objects when creating a dataset record
    fails, so a failed Add never leaves orphaned ``datasets/{id}/`` artifacts
    behind (Requirement 4.5). Deleting a key that does not exist is a no-op in
    S3 (``DeleteObject`` succeeds either way), so callers can delete
    optimistically without first checking existence.

    Args:
        key: The S3 object key (always built via :mod:`app.storage.keys`).

    The target bucket is ``Settings.s3_bucket``.
    """
    client = _get_s3_client()
    bucket = get_settings().s3_bucket
    client.delete_object(Bucket=bucket, Key=key)
    logger.debug("Deleted s3://%s/%s", bucket, key)


def presign_put(
    key: str,
    *,
    content_type: str,
    content_length: int,
    expires_in: int,
) -> str:
    """Return a pre-signed PUT URL for *key* that S3 enforces a content length on.

    Used by ``POST /uploads`` (dataset-ingestion task 8.1) so the browser can
    upload a CSV/TSV straight to S3 without the file passing through the API
    (Lambda's 6 MB request cap sits below the 10 MB upload limit). The signed
    request pins both the ``Content-Type`` and the exact ``Content-Length``:
    because both are signed parameters, S3 rejects an upload whose headers do
    not match, so the ``MAX_UPLOAD_MB`` limit is enforced server-side in
    addition to the API's own pre-issue size check.

    Args:
        key: The object key (always built via :mod:`app.storage.keys`, e.g.
            ``keys.upload_file(upload_id)``).
        content_type: The MIME type the client must send (e.g. ``text/csv``).
        content_length: The exact byte count the client must upload; signed so
            S3 refuses a larger or smaller body.
        expires_in: URL lifetime in seconds (the design uses 15 minutes).

    Returns:
        A pre-signed HTTPS URL the browser issues a ``PUT`` to.

    The target bucket is ``Settings.s3_bucket``.
    """
    client = _get_s3_client()
    bucket = get_settings().s3_bucket
    url: str = client.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": content_length,
        },
        ExpiresIn=expires_in,
        HttpMethod="PUT",
    )
    logger.debug(
        "Presigned PUT for s3://%s/%s (len=%d, expires=%ds)",
        bucket,
        key,
        content_length,
        expires_in,
    )
    return url


def presign_get(key: str, *, expires_in: int) -> str:
    """Return a pre-signed GET URL for *key* in the application bucket.

    Used by ``GET /datasets/{id}/snapshot-url`` (ingestion-summary task 1.1) so
    the browser can load a dataset's page snapshot straight from S3 through a
    short-lived link, without the PNG passing through the API. The signed URL
    grants read access to exactly one object for ``expires_in`` seconds; it does
    not require the object to exist at signing time (S3 returns the object, or a
    ``NoSuchKey`` error, when the browser follows the link).

    Args:
        key: The object key (always built via :mod:`app.storage.keys`, e.g.
            ``keys.dataset_snapshot(dataset_id, version)``).
        expires_in: URL lifetime in seconds (the design uses 5 minutes).

    Returns:
        A pre-signed HTTPS URL the browser issues a ``GET`` to.

    The target bucket is ``Settings.s3_bucket``.
    """
    client = _get_s3_client()
    bucket = get_settings().s3_bucket
    url: str = client.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
        HttpMethod="GET",
    )
    logger.debug("Presigned GET for s3://%s/%s (expires=%ds)", bucket, key, expires_in)
    return url


def list_keys(prefix: str, *, start_after: str = "") -> list[str]:
    """Return the object keys under *prefix*, lexicographically ascending.

    A thin wrapper over ``ListObjectsV2`` with the client's paginator so a
    prefix with more than 1,000 objects is fully enumerated. ``start_after``
    passes S3's ``StartAfter`` so a caller can page past a cursor key without
    re-listing earlier objects.

    The chat uses this to find a dataset's saved Exchange objects under
    ``datasets/{id}/chat/`` (keys from :mod:`app.storage.keys`), whose ISO-8601
    timestamp prefix makes this ascending listing chronological.

    Args:
        prefix: The key prefix to list (always built via
            :mod:`app.storage.keys`, e.g. ``keys.dataset_chat_prefix(id)``).
        start_after: Return only keys that sort strictly after this one. Empty
            (the default) lists from the beginning of the prefix.

    Returns:
        The matching object keys, ascending. Empty when nothing matches.

    The target bucket is ``Settings.s3_bucket``.
    """
    client = _get_s3_client()
    bucket = get_settings().s3_bucket
    paginator = client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix, StartAfter=start_after)
    found: list[str] = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if key is not None:
                found.append(key)
    logger.debug("Listed %d keys under s3://%s/%s", len(found), bucket, prefix)
    return found


def get_text(key: str, *, encoding: str = "utf-8") -> str:
    """Read the object stored under *key* and decode it as text.

    A thin convenience over :func:`get_bytes` for callers that stored text
    (for example the rendered ``page.html`` the check handler reads back for
    viability assessment).

    Args:
        key: The S3 object key (always built via :mod:`app.storage.keys`).
        encoding: Text encoding to decode with (default UTF-8).

    Returns:
        The decoded object contents.
    """
    return get_bytes(key).decode(encoding)
