"""Ingestion Summary read helpers (ingestion-summary task 1).

This module owns the server side of the dataset detail page's summary section
(design "API endpoints"). Its first endpoint helper is the page snapshot
pre-signed URL (Requirement 3.1):

- :func:`snapshot_version` picks which version's snapshot to show — the active
  version, or version 1 while the first version is still processing — as a pure
  function so the rule can be unit-tested without a database or S3.
- :func:`snapshot_url` loads the dataset, applies that rule, and mints a
  short-lived pre-signed GET URL for ``datasets/{id}/snapshot/v{n}.png``.

Its second endpoint helper is the paginated reviews table (Requirements 5.1,
5.2):

- :func:`list_reviews` reads the ``active_version`` reviews file through an
  in-memory read-through cache keyed by ``(id, version)``, filters by rating,
  sentiment, and a text search over review text, and paginates on the server,
  returning ``{items, total, page}``. The cache is a performance aid, not
  correctness-critical state: a cold start re-reads the immutable
  ``reviews/v{n}.json`` from S3. :func:`filter_reviews` and :func:`paginate` are
  pure helpers so the filter and page-boundary logic are unit-testable offline.
  When the file is absent while the dataset's status is ``updated`` the helper
  raises :class:`DataMissingError` (design "Error Handling": 500
  ``DATA_MISSING``).

Engineering rules honoured:

- **Stateless.** Pure reads against Aurora via :func:`app.core.db.session_scope`;
  the only "state" is a pre-signed URL handed back to the caller.
- **S3 keys only from ``storage.keys``.** The snapshot key is built with
  :func:`app.storage.keys.dataset_snapshot`; this module never assembles a key
  by hand.
- **DB only through ``core.db``.** The dataset is read inside ``session_scope``.
- No status change happens here (a snapshot read is not a lifecycle event), so
  nothing flows through :func:`app.db.status.transition`.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.db import session_scope
from app.core.errors import AppError, NotFoundError
from app.db.models import Dataset, DatasetStatus, SourceType
from app.storage import keys, s3

#: The page snapshot pre-signed URL lifetime: 5 minutes (design "API endpoints":
#: "valid for 5 minutes"). Kept short so a leaked URL expires quickly.
SNAPSHOT_URL_TTL_SECONDS: int = 5 * 60

#: Default and maximum page sizes for the reviews table (design "API endpoints":
#: ``page_size=25``). The cap keeps a single response bounded regardless of what
#: a client requests.
REVIEWS_DEFAULT_PAGE_SIZE: int = 25
REVIEWS_MAX_PAGE_SIZE: int = 100


class DataMissingError(AppError):
    """The ``active_version`` reviews file is absent while the dataset is updated.

    Design "Error Handling": "A reviews JSON file missing for ``active_version``
    while the status is ``updated`` returns 500 with code ``DATA_MISSING``." This
    is a server-side invariant breach — review-analysis is supposed to have
    written ``reviews/v{n}.json`` before promoting the version — so it is a 500,
    not a client error.
    """

    code = "DATA_MISSING"
    status_code = 500


@dataclass(frozen=True)
class SnapshotUrl:
    """The ``GET /datasets/{id}/snapshot-url`` response (design "API endpoints").

    - ``url``: a short-lived pre-signed GET URL for the chosen version's PNG.
    - ``expires_at``: ISO-8601 instant at which ``url`` stops working, so the
      client can refetch before it expires.
    - ``version``: the version whose snapshot ``url`` points at, so the client
      can tell which capture it is showing.
    """

    url: str
    expires_at: str
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "expires_at": self.expires_at, "version": self.version}


def snapshot_version(active_version: int | None) -> int:
    """Return which version's snapshot the detail page should show.

    Requirement 3.1: the page shows "the PNG snapshot for the active version (or
    for the first version while it is still processing)". So:

    - When the dataset has an ``active_version``, use it — that is the version
      whose metrics the summary is showing.
    - While there is no active version yet (the first version is still
      processing), fall back to version ``1``, whose snapshot was copied into
      ``snapshot/v1.png`` when the dataset was created from the Check capture.

    This is a pure function of ``active_version`` so the rule is unit-testable
    without a database.
    """
    return active_version if active_version is not None else 1


def snapshot_url(dataset_id: str) -> SnapshotUrl:
    """Return a 5-minute pre-signed URL for a URL dataset's page snapshot.

    Flow (design "API endpoints"; Requirement 3.1):

    1. Load the dataset; ``404 NOT_FOUND`` when the id is unknown.
    2. ``404 NOT_FOUND`` when the dataset is an **upload** — uploads have no page
       snapshot (ingestion stores none for them), so there is nothing to sign.
    3. Choose the version with :func:`snapshot_version` (active version, or v1
       while the first version is still processing).
    4. Build the key with :func:`app.storage.keys.dataset_snapshot` and mint a
       pre-signed GET valid for :data:`SNAPSHOT_URL_TTL_SECONDS`.

    The URL is signed even if the object is not present yet; the client's image
    load fails and the ``SnapshotCard`` shows its placeholder (Requirement 3.3),
    rather than this endpoint doing a HEAD on every request.

    Raises:
        NotFoundError: When the id is unknown, or the dataset is an upload
            (``404``).
    """
    with session_scope() as session:
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:
            raise NotFoundError("Dataset not found")
        if dataset.source_type == SourceType.UPLOAD:
            raise NotFoundError("This dataset has no page snapshot")
        version = snapshot_version(dataset.active_version)

    key = keys.dataset_snapshot(dataset_id, version)
    url = s3.presign_get(key, expires_in=SNAPSHOT_URL_TTL_SECONDS)
    expires_at = (datetime.now(UTC) + timedelta(seconds=SNAPSHOT_URL_TTL_SECONDS)).isoformat()
    return SnapshotUrl(url=url, expires_at=expires_at, version=version)


# ---------------------------------------------------------------------------
# Reviews table: GET /datasets/{id}/reviews (Requirements 5.1, 5.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewsPage:
    """The ``GET /datasets/{id}/reviews`` response (design "API endpoints").

    - ``items``: the reviews on the requested page, after filtering, each a
      dict exposing ``text``, ``rating``, ``date``, ``author``, and ``sentiment``
      (plus ``id`` and ``title`` carried through), per Requirement 5.1.
    - ``total``: how many reviews matched the filters across all pages, so the
      client can render the pager.
    - ``page``: the (1-based) page number these items came from.
    """

    items: list[dict[str, Any]]
    total: int
    page: int

    def to_dict(self) -> dict[str, Any]:
        return {"items": self.items, "total": self.total, "page": self.page}


# In-memory read-through cache of parsed reviews files, keyed by ``(id, version)``.
#
# Design "API endpoints": the endpoint "keeps it in Lambda memory by
# ``(id, version)``". This is a performance cache, **not** correctness-critical
# state (tech rule: stateless services): a cold start simply re-reads the
# immutable ``reviews/v{n}.json`` from S3. The key includes the version and that
# object is write-once per version, so a cached entry never goes stale — a new
# version is a new key. A lock guards the dict so concurrent requests in the
# same process stay consistent.
_reviews_cache: dict[tuple[str, int], list[dict[str, Any]]] = {}
_reviews_cache_lock = threading.Lock()


def reset_reviews_cache() -> None:
    """Clear the in-memory reviews cache.

    Intended for tests that swap S3 contents between cases; production never
    needs this because a given ``(id, version)`` object is immutable.
    """
    with _reviews_cache_lock:
        _reviews_cache.clear()


def _load_reviews(dataset_id: str, version: int, *, status: DatasetStatus) -> list[dict[str, Any]]:
    """Return the parsed ``reviews`` list for a version, via the cache then S3.

    On a cache miss, reads ``datasets/{id}/reviews/v{n}.json`` (key from
    :func:`app.storage.keys.dataset_reviews`) and caches the ``reviews`` array.

    Raises:
        DataMissingError: When the object is absent while *status* is
            ``updated`` (design "Error Handling": 500 ``DATA_MISSING``).
    """
    cache_key = (dataset_id, version)
    with _reviews_cache_lock:
        cached = _reviews_cache.get(cache_key)
    if cached is not None:
        return cached

    key = keys.dataset_reviews(dataset_id, version)
    if not s3.object_exists(key):
        if status == DatasetStatus.UPDATED:
            raise DataMissingError(
                f"Reviews data for the active version (v{version}) is missing; "
                "try refreshing this dataset"
            )
        # No active version promoted yet (first version still processing): no
        # data to show, but not a server invariant breach.
        return []

    doc: dict[str, Any] = json.loads(s3.get_text(key))
    reviews: list[dict[str, Any]] = list(doc.get("reviews", []))
    with _reviews_cache_lock:
        _reviews_cache[cache_key] = reviews
    return reviews


def _matches(
    review: dict[str, Any],
    *,
    rating: int | None,
    sentiment: str | None,
    query: str | None,
) -> bool:
    """Return True when *review* passes every active filter (Requirement 5.2).

    An unset filter (``None``) does not constrain. ``rating`` matches the exact
    star value; ``sentiment`` matches the label case-insensitively; ``query``
    is a case-insensitive substring search over the review text.
    """
    if rating is not None and review.get("rating") != rating:
        return False
    if sentiment is not None:
        review_sentiment = review.get("sentiment")
        if not isinstance(review_sentiment, str) or review_sentiment.lower() != sentiment.lower():
            return False
    if query:
        text = review.get("text")
        if not isinstance(text, str) or query.lower() not in text.lower():
            return False
    return True


def filter_reviews(
    reviews: list[dict[str, Any]],
    *,
    rating: int | None = None,
    sentiment: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Return the reviews matching every active filter, preserving order.

    Pure function of its inputs so the filter logic is unit-testable without S3
    or a database. Order is the stored order of the reviews file, which keeps
    pagination stable (Correctness Property 1).
    """
    return [
        review
        for review in reviews
        if _matches(review, rating=rating, sentiment=sentiment, query=query)
    ]


def paginate(items: list[dict[str, Any]], *, page: int, page_size: int) -> list[dict[str, Any]]:
    """Return the slice of *items* for a 1-based *page* of *page_size*.

    A page past the end returns an empty list rather than an error, so the
    client can over-request without failing. Pure function so the boundary is
    unit-testable.
    """
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end]


def list_reviews(
    dataset_id: str,
    *,
    page: int = 1,
    page_size: int = REVIEWS_DEFAULT_PAGE_SIZE,
    rating: int | None = None,
    sentiment: str | None = None,
    query: str | None = None,
) -> ReviewsPage:
    """Return a filtered, paginated page of a dataset's active-version reviews.

    Flow (design "API endpoints"; Requirements 5.1, 5.2):

    1. Load the dataset; ``404 NOT_FOUND`` when the id is unknown.
    2. Read the ``active_version`` reviews file through the in-memory cache
       (keyed by ``(id, version)``), re-reading from S3 on a miss. When there is
       no active version yet, there are no reviews to show (empty page).
    3. Filter by rating, sentiment, and a text search over review text.
    4. Paginate the filtered list on the server; ``total`` is the match count.

    Args:
        dataset_id: The dataset to read.
        page: 1-based page number (clamped to ``>= 1``).
        page_size: Items per page (clamped to ``1..REVIEWS_MAX_PAGE_SIZE``).
        rating: Exact star rating to filter on, or ``None`` for any.
        sentiment: Sentiment label to filter on, or ``None`` for any.
        query: Case-insensitive substring to search review text for, or ``None``.

    Raises:
        NotFoundError: When the dataset id is unknown (``404``).
        DataMissingError: When the active-version reviews file is absent while
            the dataset's status is ``updated`` (``500``).
    """
    page = max(1, page)
    page_size = max(1, min(page_size, REVIEWS_MAX_PAGE_SIZE))

    with session_scope() as session:
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:
            raise NotFoundError("Dataset not found")
        active_version = dataset.active_version
        status = dataset.status

    if active_version is None:
        # No active version promoted yet: nothing to show (the detail page shows
        # skeletons in this state), without touching S3.
        return ReviewsPage(items=[], total=0, page=page)

    reviews = _load_reviews(dataset_id, active_version, status=status)
    matched = filter_reviews(reviews, rating=rating, sentiment=sentiment, query=query)
    items = paginate(matched, page=page, page_size=page_size)
    return ReviewsPage(items=items, total=len(matched), page=page)
