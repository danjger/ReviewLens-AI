"""S3 key builders for ReviewLens AI.

This is the **only** module that constructs S3 object keys.  Every other
module that needs to read or write to S3 must import from here rather than
building paths by hand, so that the key layout can be changed in one place.

Key layout
----------
Temporary (deleted automatically after 1 day via S3 lifecycle rules):
    checks/{check_id}/{item_id}/page.html
    checks/{check_id}/{item_id}/snapshot.png
    checks/{check_id}/{item_id}/plan.json
    uploads/{upload_id}/file
    uploads/{upload_id}/mapping.json

Permanent dataset artifacts (all under ``datasets/{dataset_id}/``):
    datasets/{id}/raw/v{n}/page-{k}.html
    datasets/{id}/raw/v{n}/plan.json
    datasets/{id}/raw/v{n}/upload.csv
    datasets/{id}/raw/v{n}/mapping.json
    datasets/{id}/snapshot/v{n}.png
    datasets/{id}/reviews/v{n}.json
    datasets/{id}/chat/{iso_ts}-{uuid}.json
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Temporary check artifacts
# ---------------------------------------------------------------------------


def check_prefix(check_id: str, item_id: str) -> str:
    """The common prefix for all temporary artifacts of one check item.

    Stored on the Check Session item as ``capture_prefix`` so the handler and
    Add both know where this item's HTML, screenshot, and plan live. Objects
    under ``checks/`` are deleted after 1 day by a lifecycle rule.
    """
    return f"checks/{check_id}/{item_id}/"


def check_page(check_id: str, item_id: str) -> str:
    """Rendered HTML captured during a URL check.

    Lives under ``checks/`` and is deleted after 1 day by a lifecycle rule.
    """
    return f"checks/{check_id}/{item_id}/page.html"


def check_snapshot(check_id: str, item_id: str) -> str:
    """Above-the-fold screenshot taken during a URL check.

    Lives under ``checks/`` and is deleted after 1 day by a lifecycle rule.
    """
    return f"checks/{check_id}/{item_id}/snapshot.png"


def check_plan(check_id: str, item_id: str) -> str:
    """Extraction plan produced during a URL check.

    Lives under ``checks/`` and is deleted after 1 day by a lifecycle rule.
    """
    return f"checks/{check_id}/{item_id}/plan.json"


# ---------------------------------------------------------------------------
# Temporary upload staging
# ---------------------------------------------------------------------------


def upload_file(upload_id: str) -> str:
    """Pending CSV uploaded via pre-signed PUT.

    Lives under ``uploads/`` and is deleted after 1 day by a lifecycle rule.
    """
    return f"uploads/{upload_id}/file"


def upload_mapping(upload_id: str) -> str:
    """Confirmed column mapping staged next to a pending upload.

    The Library's "refresh by upload" flow (dataset-library) stages the
    analyst-confirmed mapping JSON here so the shared Refresh Service has a
    concrete ``mapping_key`` to copy into the dataset's permanent
    ``raw/v{n}/mapping.json`` — mirroring the per-dataset
    :func:`dataset_raw_mapping` destination. Lives under ``uploads/`` and is
    deleted after 1 day by a lifecycle rule if the refresh never completes.
    """
    return f"uploads/{upload_id}/mapping.json"


# ---------------------------------------------------------------------------
# Permanent dataset artifacts
# ---------------------------------------------------------------------------


def dataset_prefix(dataset_id: str) -> str:
    """The common prefix for all permanent artifacts of a dataset.

    All permanent dataset keys start with this prefix.
    """
    return f"datasets/{dataset_id}/"


def dataset_raw_page(dataset_id: str, version: int, page_num: int) -> str:
    """Captured HTML for a single page of a dataset version."""
    return f"datasets/{dataset_id}/raw/v{version}/page-{page_num}.html"


def dataset_raw_plan(dataset_id: str, version: int) -> str:
    """Extraction plan used for a dataset version."""
    return f"datasets/{dataset_id}/raw/v{version}/plan.json"


def dataset_raw_upload(dataset_id: str, version: int) -> str:
    """Uploaded CSV file for an upload-type dataset version."""
    return f"datasets/{dataset_id}/raw/v{version}/upload.csv"


def dataset_raw_mapping(dataset_id: str, version: int) -> str:
    """Column mapping JSON for an upload-type dataset version."""
    return f"datasets/{dataset_id}/raw/v{version}/mapping.json"


def dataset_snapshot(dataset_id: str, version: int) -> str:
    """Above-the-fold screenshot for a dataset version."""
    return f"datasets/{dataset_id}/snapshot/v{version}.png"


def dataset_reviews(dataset_id: str, version: int) -> str:
    """Normalized reviews plus entity profile for a dataset version."""
    return f"datasets/{dataset_id}/reviews/v{version}.json"


def dataset_chat_prefix(dataset_id: str) -> str:
    """The common prefix for all saved Q&A exchanges of a dataset.

    Listing this prefix returns the ``{iso_ts}-{uuid}.json`` exchange objects in
    chronological order, because the ISO-8601 timestamp prefix sorts
    lexicographically by time. Used by the chat to load a conversation's recent
    exchanges and by the history API.
    """
    return f"datasets/{dataset_id}/chat/"


def dataset_chat_exchange(dataset_id: str, iso_ts: str, exchange_id: str) -> str:
    """One Q&A exchange for a dataset.

    ``iso_ts`` should be an ISO-8601 timestamp string (e.g.
    ``2024-01-15T12:34:56Z``) and ``exchange_id`` a UUID string.
    """
    return f"datasets/{dataset_id}/chat/{iso_ts}-{exchange_id}.json"
