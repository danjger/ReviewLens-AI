"""Ingestion Summary API router (ingestion-summary task 1).

The detail page's summary section reads from a few endpoints beyond the Library
record (design "API endpoints"). This router carries those ingestion-summary
additions, mounted under the ``/api`` prefix by :mod:`app.api`. The first is the
page snapshot pre-signed URL:

- ``GET /datasets/{id}/snapshot-url`` — ``{url, expires_at, version}``: a
  pre-signed GET URL for the active version's snapshot (or version 1 while the
  first version is still processing), valid for 5 minutes; ``404`` for uploads
  and for unknown ids (Requirement 3.1).
- ``GET /datasets/{id}/reviews?page&page_size&rating&sentiment&q`` —
  ``{items, total, page}``: a filtered, server-paginated page of the active
  version's reviews (Requirements 5.1, 5.2). ``404`` for unknown ids; ``500``
  with code ``DATA_MISSING`` when the active-version reviews file is absent.

All domain logic lives in :mod:`app.datasets.summary`; this router is a thin
controller that delegates and serializes. It raises the shared
:class:`~app.core.errors.AppError` subclasses so the
``{"error": {"code", "message"}}`` envelope is produced by the app's error
handlers. It is plain FastAPI with no Lambda event shape, so it runs unchanged
in both compute modes.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.datasets import summary

router = APIRouter(prefix="/datasets", tags=["ingestion-summary"])


@router.get("/{dataset_id}/snapshot-url")
def get_snapshot_url(dataset_id: str) -> dict[str, object]:
    """Return a 5-minute pre-signed URL for a dataset's page snapshot.

    Returns ``{url, expires_at, version}`` for the active version's snapshot, or
    version 1's while the first version is still processing (Requirement 3.1).
    Raises ``404 NOT_FOUND`` when the id is unknown or the dataset is an upload
    (uploads have no page snapshot).
    """
    return summary.snapshot_url(dataset_id).to_dict()


@router.get("/{dataset_id}/reviews")
def list_reviews(
    dataset_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=summary.REVIEWS_DEFAULT_PAGE_SIZE,
        ge=1,
        le=summary.REVIEWS_MAX_PAGE_SIZE,
    ),
    rating: int | None = Query(default=None, ge=1, le=5),
    sentiment: str | None = Query(default=None),
    q: str | None = Query(default=None),
) -> dict[str, object]:
    """Return a filtered, paginated page of a dataset's active-version reviews.

    Returns ``{items, total, page}`` where ``items`` are the reviews on this
    page (each with text, rating, date, author, and sentiment), filtered by
    ``rating``, ``sentiment``, and a text search ``q`` over review text, then
    paginated on the server (Requirements 5.1, 5.2).

    Raises ``404 NOT_FOUND`` when the id is unknown and ``500 DATA_MISSING``
    when the active-version reviews file is absent while the dataset is updated.
    """
    return summary.list_reviews(
        dataset_id,
        page=page,
        page_size=page_size,
        rating=rating,
        sentiment=sentiment,
        query=q,
    ).to_dict()
