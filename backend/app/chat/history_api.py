"""Chat history API router (guardrailed-chat Task 5.1).

The shared Q&A history endpoint lives on the **API service** (design
"Endpoints": "The history, save, and suggestions endpoints are ordinary routes
on the API service"; only the streaming chat ``POST`` lives on the chat
service). This router is mounted under the ``/api`` prefix by :mod:`app.api`,
and its ``/datasets`` prefix makes the final path
``/api/datasets/{id}/chat/history``.

- ``GET /datasets/{id}/chat/history?before={ts}&limit=20`` — the merged,
  oldest-first timeline of Exchanges and refresh markers (Requirements 5.2,
  5.3, 5.6, 9.1–9.4, 9.7, 9.8). Returns ``{items, next_before, has_more}``;
  ``before`` is the backward-paging cursor returned as ``next_before`` from the
  newer page, and ``limit`` defaults to :data:`app.chat.history.PAGE_SIZE`.

All timeline logic lives in :mod:`app.chat.history`; this router is a thin
controller that validates the query parameters, delegates, and serializes. It
is plain FastAPI with no Lambda event shape, so it runs unchanged in both
compute modes.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.chat import history

router = APIRouter(prefix="/datasets", tags=["chat-history"])


@router.get("/{dataset_id}/chat/history")
def get_chat_history(
    dataset_id: str,
    before: str | None = Query(default=None),
    limit: int = Query(default=history.PAGE_SIZE, ge=1, le=history.PAGE_SIZE),
) -> dict[str, object]:
    """Return one page of the dataset's shared Q&A timeline, oldest first.

    Merges saved Exchanges with refresh markers from ``dataset_versions`` and
    flags each Exchange ``is_stale`` when it was answered against a version older
    than the dataset's ``active_version`` (Requirement 9.4). Pages backward by
    the shared ``before`` timestamp cursor in pages of
    :data:`~app.chat.history.PAGE_SIZE` (Requirement 5.3); omit ``before`` for
    the newest page. An unknown id yields an empty page (the no-sign-in app does
    not leak whether an id exists).
    """
    return history.load_history(dataset_id, before=before, limit=limit).to_dict()
