"""Chat retry-save API router (guardrailed-chat Task 5.2).

The chat service streams each answered Exchange to the browser in the terminal
``done`` SSE event with ``saved: false`` when the first S3 save failed (design
"Error Handling"). The browser then shows a Retry button that replays that
exact payload to this endpoint, which persists it (Requirement 5.5).

Because the app has no sign-in, anything the browser can replay a visitor could
also *forge*. The service signs every Exchange's ``done`` payload with a server
HMAC secret (Task 4.3, :mod:`app.chat.signing`), and this endpoint refuses any
payload whose signature doesn't verify — so visitors can't write made-up or
edited Exchanges into the shared history (design "Endpoints"; Correctness
Property 4: "Saved history can't be forged").

Like the history and suggestions endpoints, the retry-save endpoint is an
**ordinary API-service route** (design "Endpoints": "The history, save, and
suggestions endpoints are ordinary routes on the API service"; only the
streaming chat ``POST`` lives on the chat service). It is mounted under ``/api``
by :mod:`app.api`, and its ``/datasets`` prefix makes the final path
``POST /api/datasets/{id}/chat/save``. The origin guard already runs as
middleware on the API app, so no per-route check is needed here.

Refusal status code
-------------------
An invalid or missing signature is refused with **403 FORBIDDEN**. The design
says the endpoint "refuses anything whose signature doesn't match"; a forged or
edited payload is an authorization failure (the caller lacks the server secret),
so 403 — not 422 — is the right envelope. A path/body ``dataset_id`` mismatch is
treated the same way: a signed payload is bound to its own ``dataset_id``, so a
mismatch means the signature does not cover this path and the write is refused.

Avoiding an import cycle
------------------------
This router deliberately does **not** import :mod:`app.chat.service`. That module
is the streaming FastAPI app and pulls in the AI client, pre-check, assembly, and
the whole chat runtime; importing it into the API service would create a heavy,
cyclic dependency and load the streaming app into the API process. Instead this
module reuses only the small, cycle-free pieces — :mod:`app.chat.signing`,
:mod:`app.storage.keys`, :mod:`app.storage.s3`, and
:func:`app.events.publisher.publish_event` — and re-declares the
``chat.exchange.saved`` detail-type string locally (it also lives as
``service._EXCHANGE_SAVED_EVENT``; the two are deliberately kept identical and
each carries this note). Both the service's ``_persist_exchange`` and this
endpoint therefore write the same object and publish the same event, by the same
means, without one importing the other.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Response

from app.chat import signing
from app.core.config import get_settings
from app.core.errors import AppError
from app.events.publisher import publish_event
from app.storage import keys, s3

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/datasets", tags=["chat-save"])

#: Error code returned when a save is refused (bad/missing signature or a
#: path/body ``dataset_id`` mismatch). ``UPPER_SNAKE_CASE`` per repo convention.
SAVE_REFUSED_CODE = "EXCHANGE_SIGNATURE_INVALID"

#: EventBridge detail-type published after a successful (re-)save so the push
#: consumer fans the Exchange out to every browser viewing the dataset
#: (Requirement 5.7). The event body carries IDs only (steering: "queue/event
#: bodies carry IDs"). Deliberately identical to
#: ``app.chat.service._EXCHANGE_SAVED_EVENT`` — the API service must not import
#: the streaming service module (see the module docstring), so the string is
#: re-declared here rather than imported.
_EXCHANGE_SAVED_EVENT = "chat.exchange.saved"

#: Content type for the saved Exchange S3 object (matches ``service``).
_EXCHANGE_CONTENT_TYPE = "application/json"


class _SaveRefusedError(AppError):
    """Raised when a retry-save payload can't be trusted (HTTP 403).

    Covers a missing/invalid HMAC signature and a path/body ``dataset_id``
    mismatch. Mapped to the shared ``{"error": {"code", "message"}}`` envelope
    by the API app's ``AppError`` handler.
    """

    code = SAVE_REFUSED_CODE
    status_code = 403


@router.post("/{dataset_id}/chat/save")
def save_exchange(dataset_id: str, exchange: dict[str, Any]) -> Response:
    """Persist a previously unsaved Exchange, if its HMAC signature verifies.

    The body is the Exchange exactly as the chat service sent it in the ``done``
    event, including the ``signature`` field. The endpoint:

    1. Refuses (**403**) when the body's ``dataset_id`` does not match the path
       — a signed Exchange is bound to its own dataset, so a mismatch can't be
       trusted (and avoids writing an Exchange under the wrong dataset's prefix).
    2. Refuses (**403**) when the signature is missing or does not verify against
       the server secret (``signing.verify_exchange`` — the exact scheme from
       Task 4.3, which excludes ``signature`` and ``saved`` from the signed
       content). This is what stops a visitor forging or editing history.
    3. On a valid signature, writes the Exchange JSON to
       ``datasets/{id}/chat/{asked_at}-{id}.json`` (key from
       ``storage.keys.dataset_chat_exchange``, the same key the service uses),
       with ``saved: true``, then publishes ``chat.exchange.saved`` (IDs only)
       and returns the saved Exchange.

    Idempotency: a retry that re-saves the same valid Exchange re-writes the same
    key (``put_object`` overwrites with identical bytes) and re-publishes the
    event. Overwriting the same key is safe; a duplicate ``chat.exchange.saved``
    is tolerable because the push consumer is idempotent per ``exchange_id`` (it
    fans out an Exchange the browser already has), so a double-publish only
    re-notifies and never corrupts the shared history.
    """
    settings = get_settings()

    # (1) The signed payload is bound to its own dataset_id. A path mismatch
    # means the signature does not cover this path — refuse rather than write
    # the Exchange under the wrong dataset's prefix.
    body_dataset_id = exchange.get("dataset_id")
    if body_dataset_id != dataset_id:
        logger.warning(
            "chat_save_refused reason=dataset_mismatch path_id=%s body_id=%s",
            dataset_id,
            body_dataset_id,
            extra={"event": "chat_save_refused", "reason": "dataset_mismatch"},
        )
        raise _SaveRefusedError("This exchange can't be saved to this dataset.")

    # (2) Verify the HMAC with the exact Task 4.3 scheme. A missing/edited/forged
    # signature is refused — nothing is written and no event is published.
    if not signing.verify_exchange(exchange, settings.chat_signing_secret):
        logger.warning(
            "chat_save_refused reason=bad_signature dataset_id=%s exchange_id=%s",
            dataset_id,
            exchange.get("id"),
            extra={"event": "chat_save_refused", "reason": "bad_signature"},
        )
        raise _SaveRefusedError("This exchange couldn't be verified and was not saved.")

    # (3) Signature valid → persist with saved:true at the chronological key.
    exchange_id = exchange["id"]
    iso_ts = exchange["asked_at"]
    key = keys.dataset_chat_exchange(dataset_id, iso_ts, exchange_id)

    exchange["saved"] = True
    s3.put_bytes(
        key,
        json.dumps(exchange, default=str).encode("utf-8"),
        content_type=_EXCHANGE_CONTENT_TYPE,
    )

    publish_event(
        detail_type=_EXCHANGE_SAVED_EVENT,
        detail={"dataset_id": dataset_id, "exchange_id": exchange_id, "key": key},
    )
    logger.info(
        "chat_exchange_resaved dataset_id=%s exchange_id=%s",
        dataset_id,
        exchange_id,
        extra={"event": "chat_exchange_resaved", "dataset_id": dataset_id},
    )
    return Response(
        content=json.dumps(exchange, default=str),
        media_type=_EXCHANGE_CONTENT_TYPE,
        status_code=200,
    )
