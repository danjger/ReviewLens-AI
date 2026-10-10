"""Unit tests for ``app.capture.from_upload`` (dataset-ingestion task 17.1).

Covers Requirements 8.2, 8.3, 8.5, and 4.3 for the upload-backed capture:

- :func:`from_upload` reads ``uploads/{upload_id}/file`` from S3 and builds a
  :class:`~app.ingestion.viability.CaptureView` with the uploaded markup as
  ``html``, a synthetic ``main_status = 200`` (so the "not 200 → wont_work"
  rule never misfires on an upload, Requirement 2.7/8.3), and the ``<title>``
  parsed from the markup (Requirement 4.3);
- :func:`synthesize_final_url` returns the analyst source URL when supplied, else
  the ``upload://{upload_id}`` placeholder used only as plan context and never
  fetched (Requirements 8.5, 8.8);
- a file that is not readable text is rejected with a specific message **and the
  staged object is deleted**, mirroring the bad-CSV handling (Requirement 8.2 /
  7.3).

The S3-backed read/delete is exercised under moto so the real
``keys.upload_file`` key and the delete-on-invalid cleanup run against a genuine
bucket, matching ``tests/unit/ingestion/test_upload_parser.py``.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from app.capture.upload import (
    UploadNotReadableError,
    from_upload,
    synthesize_final_url,
)
from app.core.config import get_settings
from app.storage import keys
from app.storage import s3 as s3_mod
from moto import mock_aws

_BUCKET = "reviewlens-test"


@pytest.fixture()
def s3_bucket(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A moto-backed S3 bucket so ``from_upload`` reads/deletes real objects."""
    monkeypatch.setenv("S3_BUCKET", _BUCKET)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "")
    get_settings.cache_clear()
    s3_mod.reset_client()
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET)
        yield client
    get_settings.cache_clear()
    s3_mod.reset_client()


def _put(client: Any, upload_id: str, raw: bytes) -> str:
    key = keys.upload_file(upload_id)
    client.put_object(Bucket=_BUCKET, Key=key, Body=raw)
    return key


_HTML = (
    b"<!doctype html><html><head><title>  Acme Widget  Reviews  </title></head>"
    b"<body><div class='review'>Great widget, five stars.</div>"
    b"<div class='review'>Shipping was slow but the product is solid.</div>"
    b"</body></html>"
)


# ---------------------------------------------------------------------------
# synthesize_final_url (Requirements 8.5, 8.8)
# ---------------------------------------------------------------------------


def test_synthesize_final_url_uses_source_url_when_present() -> None:
    assert (
        synthesize_final_url("u1", "https://example.com/product/42")
        == "https://example.com/product/42"
    )


@pytest.mark.parametrize("missing", [None, ""])
def test_synthesize_final_url_falls_back_to_placeholder(missing: str | None) -> None:
    assert synthesize_final_url("abc123", missing) == "upload://abc123"


# ---------------------------------------------------------------------------
# from_upload — builds the view (Requirements 8.3, 4.3)
# ---------------------------------------------------------------------------


def test_from_upload_builds_view_with_synthetic_fields_and_title(s3_bucket: Any) -> None:
    key = _put(s3_bucket, "u1", _HTML)

    view = from_upload("u1", "https://example.com/product/42")

    # The uploaded markup is carried through verbatim as the view's html.
    assert view.html == _HTML.decode("utf-8")
    # Synthetic successful render so the "main status not 200" rule never fires.
    assert view.main_status == 200
    # Title parsed from the markup, whitespace-normalized (Requirement 4.3).
    assert view.page_title == "Acme Widget Reviews"
    # A valid file is left untouched.
    assert s3_mod.object_exists(key)


def test_from_upload_without_source_url_still_builds_view(s3_bucket: Any) -> None:
    _put(s3_bucket, "u2", _HTML)

    view = from_upload("u2")

    assert view.html == _HTML.decode("utf-8")
    assert view.main_status == 200
    assert view.page_title == "Acme Widget Reviews"


def test_from_upload_missing_title_yields_empty_string(s3_bucket: Any) -> None:
    _put(s3_bucket, "u3", b"<html><body><p>reviews but no title</p></body></html>")

    view = from_upload("u3")

    assert view.page_title == ""
    assert view.main_status == 200


# ---------------------------------------------------------------------------
# from_upload — non-text file rejected and deleted (Requirement 8.2)
# ---------------------------------------------------------------------------


def test_from_upload_non_text_file_rejected_and_deleted(s3_bucket: Any) -> None:
    # Invalid UTF-8 byte sequence (e.g. a binary saved-page blob).
    key = _put(s3_bucket, "u4", b"\xff\xfe\x00\x01not-text")

    with pytest.raises(UploadNotReadableError) as exc:
        from_upload("u4", "https://example.com/x")

    # Specific, user-facing message mirroring the bad-CSV wording.
    assert "could not be read as text" in str(exc.value).lower()
    # The staged object is deleted so no orphan is left (Requirement 8.2 / 7.3).
    assert not s3_mod.object_exists(key)
