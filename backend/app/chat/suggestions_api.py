"""Chat suggestions API router (guardrailed-chat Task 5.3).

The chat panel's starter-question suggestions live on the **API service**
(design "Endpoints": "The history, save, and suggestions endpoints are ordinary
routes on the API service"; only the streaming chat ``POST`` lives on the chat
service). This router is mounted under the ``/api`` prefix by :mod:`app.api`,
and its ``/datasets`` prefix makes the final path
``/api/datasets/{id}/chat/suggestions``.

- ``GET /datasets/{id}/chat/suggestions`` — 3–4 starter questions built from the
  dataset's theme labels using templates, with general fallback questions when
  no themes are available (Requirement 1.4). **No AI call** is made.

All the template/cap/dedup logic and the themes read live in
:mod:`app.chat.suggestions`; this router is a thin controller that delegates and
serializes. It is plain FastAPI with no Lambda event shape, so it runs unchanged
in both compute modes, and it does not import :mod:`app.chat.service` (no cycle).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.chat import suggestions

router = APIRouter(prefix="/datasets", tags=["chat-suggestions"])


@router.get("/{dataset_id}/chat/suggestions")
def get_chat_suggestions(dataset_id: str) -> dict[str, list[str]]:
    """Return 3–4 suggested starter questions for the dataset (Requirement 1.4).

    Builds the questions from the active version's theme labels using templates
    (no AI call); when no themes are available (no active version, or ``metrics``
    carries no themes) returns general questions that fit any review set. An
    unknown id yields the general fallback questions (the no-sign-in app does not
    leak whether an id exists).
    """
    return {"suggestions": suggestions.load_suggestions(dataset_id)}
