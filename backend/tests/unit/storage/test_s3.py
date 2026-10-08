"""Unit tests for the S3 helpers added for the Refresh Service (task 6.1).

Covers the two helpers the Refresh Service relies on, against a moto-mocked S3
bucket:

- ``copy_object`` performs a server-side copy within the application bucket, so
  the destination holds the source bytes and the source is left in place.
- ``object_exists`` reports presence/absence without raising on a missing key.

The existing ``put_bytes`` / ``get_bytes`` round-trip underpins the assertions.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from app.core.config import get_settings
from app.storage import s3
from moto import mock_aws

_REGION = "us-east-1"
_BUCKET = "reviewlens-test-bucket"


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_BUCKET", _BUCKET)
    get_settings.cache_clear()
    s3.reset_client()
    yield
    s3.reset_client()
    get_settings.cache_clear()


def _create_bucket() -> None:
    boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)


def test_copy_object_duplicates_bytes_and_keeps_source() -> None:
    with mock_aws():
        _create_bucket()
        s3.put_bytes("checks/c1/u1/page.html", b"<html>hi</html>", content_type="text/html")

        s3.copy_object("checks/c1/u1/page.html", "datasets/d1/raw/v2/page-1.html")

        # Destination has the bytes; source is still present (server-side copy).
        assert s3.get_bytes("datasets/d1/raw/v2/page-1.html") == b"<html>hi</html>"
        assert s3.get_bytes("checks/c1/u1/page.html") == b"<html>hi</html>"


def test_object_exists_true_for_present_key() -> None:
    with mock_aws():
        _create_bucket()
        s3.put_bytes("uploads/up1/file", b"a,b,c\n1,2,3\n", content_type="text/csv")
        assert s3.object_exists("uploads/up1/file") is True


def test_object_exists_false_for_absent_key() -> None:
    with mock_aws():
        _create_bucket()
        assert s3.object_exists("checks/missing/u1/snapshot.png") is False


def test_presign_get_returns_a_url_for_the_bucket_and_key() -> None:
    """``presign_get`` signs a GET for the given key in the application bucket.

    The signed URL names the bucket and key and carries an expiry; following it
    returns the stored bytes (the snapshot PNG used by ingestion-summary task
    1.1). The object need not exist at signing time, but when it does the link
    resolves to it.
    """
    with mock_aws():
        _create_bucket()
        key = "datasets/d1/snapshot/v2.png"
        s3.put_bytes(key, b"\x89PNG\r\n", content_type="image/png")

        url = s3.presign_get(key, expires_in=300)

        assert key in url
        assert _BUCKET in url
        assert "Expires=" in url or "X-Amz-Expires=300" in url


def test_presign_get_signs_absent_key_without_raising() -> None:
    """Signing does not require the object to exist (the browser load may 404)."""
    with mock_aws():
        _create_bucket()
        url = s3.presign_get("datasets/d1/snapshot/v9.png", expires_in=300)
        assert "datasets/d1/snapshot/v9.png" in url
