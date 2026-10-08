"""Duplicate dataset lookup for the Check step (Requirement 6.2).

``find_existing(normalized_target, normalized_final)`` answers the question the
check handler asks after a page is captured: *is this URL already tracked?* A
URL is "already tracked" when its normalized Target URL **or** normalized Final
URL matches the normalized original or final URL of any existing dataset —
**including archived datasets** (Requirement 6.2). Matching an archived dataset
still counts, because adding the URL later restores and refreshes it rather
than creating a copy (Requirement 6.5).

Only URL datasets are considered: an upload dataset has no URL to match, and the
unique index that enforces "one dataset per URL" is partial on
``source_type = 'url'`` (see :mod:`app.db.models`). Upload rows therefore never
collide with a URL being checked.

The lookup reads the database only through :mod:`app.core.db` (Requirement:
"reach the database only through ``core.db``"), so it works unchanged against
the Aurora Data API in AWS and psycopg locally. It is a read-only query and
keeps no state.

A match is returned as a small :class:`ExistingDataset` value object — exactly
the fields the handler writes to the Check Session item's ``existing_dataset``
field and the UI shows on the "Already tracked as …" note (Requirement 6.3).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select

from app.core.db import session_scope
from app.db.models import Dataset, SourceType


@dataclass(frozen=True)
class ExistingDataset:
    """A tracked dataset that a checked URL already maps to.

    These are the fields the check handler records on the Check Session item's
    ``existing_dataset`` attribute and the UI reads to render the "Already
    tracked as '{name}' — adding will refresh it" note (Requirement 6.3):

    Attributes:
        id: The existing dataset's UUID.
        name: Its display name (shown on the tracked note).
        archived: True when the dataset is archived (so Add will *restore* it,
            Requirement 6.5).
        status: Its current lifecycle status (``requested`` / ``processing`` /
            ``updated`` / ``failed``), so the UI and Add can tell whether a
            refresh is already in flight (Requirement 6.6).
    """

    id: str
    name: str
    archived: bool
    status: str

    def to_dict(self) -> dict[str, object]:
        """Return the JSON shape stored on the Check Session item."""
        return {
            "id": self.id,
            "name": self.name,
            "archived": self.archived,
            "status": self.status,
        }


def find_existing(
    normalized_target: str | None,
    normalized_final: str | None,
) -> ExistingDataset | None:
    """Return the dataset a checked URL already maps to, or ``None``.

    A URL is already tracked when either of its normalized forms matches either
    normalized column of any **URL** dataset, archived included (Requirement
    6.2):

    - ``normalized_target`` or ``normalized_final`` equals a row's
      ``normalized_url`` (the normalized original URL), or
    - ``normalized_target`` or ``normalized_final`` equals a row's
      ``normalized_final_url`` (the URL a redirect landed on).

    The unique partial index guarantees at most one URL dataset per
    ``normalized_url``; when a match is found through ``normalized_final_url``
    (which is not unique) the first match is returned, which is enough for the
    "already tracked" note and for routing Add to a refresh.

    Args:
        normalized_target: The normalized Target URL (always present for a URL
            that reached capture). ``None`` is tolerated and simply contributes
            no match.
        normalized_final: The normalized Final URL (the URL that returned 200).
            ``None`` when the probe produced no distinct final URL.

    Returns:
        An :class:`ExistingDataset` for the first matching URL dataset, or
        ``None`` when the URL is not tracked.
    """
    candidates = [u for u in (normalized_target, normalized_final) if u]
    if not candidates:
        return None

    stmt = (
        select(Dataset)
        .where(
            Dataset.source_type == SourceType.URL,
            or_(
                Dataset.normalized_url.in_(candidates),
                Dataset.normalized_final_url.in_(candidates),
            ),
        )
        .limit(1)
    )

    with session_scope() as session:
        dataset = session.scalars(stmt).first()
        if dataset is None:
            return None
        return ExistingDataset(
            id=dataset.id,
            name=dataset.name,
            archived=dataset.archived_at is not None,
            status=str(dataset.status.value),
        )
