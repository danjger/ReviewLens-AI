"""Ingestion API router: the Check endpoints (dataset-ingestion task 4.3).

This router supplies the three Check endpoints from the dataset-ingestion
design's "API endpoints" table, all mounted under the ``/api`` prefix by
:mod:`app.api`:

- ``POST /api/ingest/checks`` — accept up to 10 URLs, rate-limit the request
  (per-IP and global, Requirement 1.6), parse and normalize the lines
  (Requirements 1.1, 1.2, 1.4), create a Check Session (``origin="new"``), and
  enqueue one ``check-queue`` message per pending item. Returns ``202`` with the
  per-item initial states.
- ``GET /api/ingest/checks/{check_id}`` — the polling fallback: return the
  session with every item's state, verdict, and evidence. ``404`` when the
  session has expired (Requirement 4.4). The ``#session`` header row is never
  exposed.
- ``POST /api/ingest/checks/{check_id}/items/{item_id}/retry`` — re-queue a
  single item that ended in ``error`` or timed out (Requirement 3.10). Returns
  ``202``; ``429`` when rate-limited.
- ``POST /api/ingest/checks/{check_id}/add`` — add the chosen Check items,
  returning a per-item ``outcome`` (Requirements 5.4, 6.5, 6.6). This is a thin
  controller over :func:`app.ingestion.service.add_items`, which owns all the
  per-item decisions (refuse ``wont_work``, confirm ``limited``, create vs
  refresh, the ``applied`` claim, and the unique-URL race). An expired or absent
  session yields each requested item's outcome ``expired`` and still returns
  ``200`` (per the design, Add returns outcomes rather than ``404``; the UI asks
  the analyst to check again). The design's "API endpoints" table lists no rate
  limit for Add, so none is applied here.

Engineering rules honoured here:

- **Rate limiting** goes through :func:`app.core.rate_limit.check_rate_limit`
  with the per-IP/global ``checks`` counters; the client IP is read from the
  CloudFront header and **hashed** inside the limiter, never stored or logged
  raw (privacy rule). A ``RateLimitError`` becomes a ``429`` with a
  ``Retry-After`` header.
- **Error shape** is the shared ``{"error": {"code", "message"}}`` envelope with
  ``UPPER_SNAKE_CASE`` codes (via :mod:`app.core.errors`).
- **Queue bodies** are small JSON objects with IDs only
  (``{"check_id", "item_id"}``); the worker reads the rest from the session.
- Nothing here imports a Lambda event shape; this is a plain FastAPI router that
  runs unchanged in both compute modes.

This router wires the ``origin="new"`` Check flow and ``Add`` (task 7.3). The
``refresh`` origin (task 6.2) is driven by the Library, not this router.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.errors import AppValidationError, NotFoundError, RateLimitError
from app.core.queue import enqueue
from app.core.rate_limit import check_rate_limit, get_client_ip
from app.ingestion import check_session, service, upload_parser
from app.ingestion.check_session import CheckItem, ItemState
from app.ingestion.service import AddRequestItem
from app.ingestion.upload_parser import UploadInvalidError
from app.ingestion.url_validator import parse_submission
from app.storage import keys, s3

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])

#: Uploads live at ``/api/uploads`` (design "API endpoints" table), not under
#: the Check ``/ingest`` prefix, so they get their own router mounted alongside.
uploads_router = APIRouter(prefix="/uploads", tags=["uploads"])

#: Dataset-level ingestion endpoints that are not under ``/ingest`` or
#: ``/uploads``. ``POST /datasets/upload`` submits a previewed upload as a new
#: dataset (design "API endpoints" table). The Library owns the rest of
#: ``/datasets``; this router carries only the ingestion-owned upload submit.
datasets_router = APIRouter(prefix="/datasets", tags=["datasets"])

#: The rate-limit action label for Check requests (per-IP + global ``checks``).
_CHECK_ACTION = "checks"

#: The rate-limit action label for upload requests (per-IP + global ``uploads``).
_UPLOAD_ACTION = "uploads"

#: Pre-signed PUT lifetime for uploads (design: "15 minutes").
_UPLOAD_URL_TTL_SECONDS = 15 * 60

#: Content types accepted for a tabular review upload (``.csv`` / ``.tsv``).
#: ``text/plain`` and the generic binary type are allowed too because browsers
#: frequently send those for ``.csv``/``.tsv`` files; the extension is the
#: authoritative gate (Requirement 7.1).
_ALLOWED_UPLOAD_EXTENSIONS = (".csv", ".tsv")

#: Item states a retry may re-queue (design: "ended in ``error`` or timed out").
#: ``error`` is the crashed-handler state; ``done`` is a finished item whose
#: verdict is the ``wont_work`` timeout (Requirement 3.10) — only that done case
#: is retryable, which :func:`_is_retryable_timeout` checks.
_TIMEOUT_REASON = "Page took too long to load"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateCheckRequest(BaseModel):
    """Body of ``POST /ingest/checks``: the submitted URLs (1–10 lines).

    The field is a list of strings; the panel sends one URL per entry. Parsing,
    normalization, invalid-line marking, and within-batch duplicate collapsing
    all happen in :func:`app.ingestion.url_validator.parse_submission`, so this
    model only guards the coarse 1-item lower bound (an empty submission is a
    client error). The upper bound (10) is enforced by ``parse_submission`` from
    configuration, so extra lines are ignored rather than rejected.
    """

    urls: list[str] = Field(..., min_length=1)


class CheckItemView(BaseModel):
    """One item as returned by ``POST /ingest/checks`` (the initial view)."""

    item_id: str
    input: str
    normalized: str | None = None
    state: str
    message: str | None = None


class CreateCheckResponse(BaseModel):
    """``202`` body of ``POST /ingest/checks``."""

    check_id: str
    items: list[CheckItemView]


class AddItemRequest(BaseModel):
    """One item in the ``POST /ingest/checks/{check_id}/add`` body.

    ``confirm_limited`` is the analyst's explicit confirmation to add a
    ``limited`` URL (Requirement 3.9); it is ignored for other verdicts and
    defaults to ``False``.
    """

    item_id: str
    confirm_limited: bool = False


class AddRequest(BaseModel):
    """Body of ``POST /ingest/checks/{check_id}/add``: the chosen items (1+).

    The lower bound guards an empty Add (a client error → ``422``); the per-item
    decisions all live in :func:`app.ingestion.service.add_items`.
    """

    items: list[AddItemRequest] = Field(..., min_length=1)


class CreateUploadRequest(BaseModel):
    """Body of ``POST /uploads``: the file the browser is about to upload.

    The browser sends the file's declared ``filename``, ``size_bytes``, and
    ``content_type`` so the API can refuse an over-limit or wrong-type file
    *before* issuing a pre-signed URL (design Error Handling: "Refused before a
    pre-signed URL is issued"). ``size_bytes`` has a ``gt=0`` lower bound so a
    zero/negative size is a plain ``422`` from request validation.
    """

    filename: str = Field(..., min_length=1)
    size_bytes: int = Field(..., gt=0)
    content_type: str = Field(..., min_length=1)


class CreateUploadResponse(BaseModel):
    """``201`` body of ``POST /uploads``: where and how to upload the file."""

    upload_id: str
    put_url: str
    expires_at: str


class SubmitUploadRequest(BaseModel):
    """Body of ``POST /datasets/upload`` (design "API endpoints" table).

    ``upload_id`` names the staged object; ``name`` is the analyst-supplied
    dataset name (required and non-empty — Requirement 7.4); ``mapping`` is the
    confirmed canonical-field → header-name column mapping the analyst reviewed
    in the preview; ``description`` is an optional source description.

    ``name`` carries a ``min_length=1`` bound so a missing or empty name is a
    plain ``422`` from request validation; the endpoint additionally rejects a
    whitespace-only name.
    """

    upload_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    mapping: dict[str, str] = Field(default_factory=dict)
    description: str | None = None


class SubmitUploadResponse(BaseModel):
    """``201`` body of ``POST /datasets/upload``: the new dataset's id."""

    id: str


# ---------------------------------------------------------------------------
# POST /ingest/checks
# ---------------------------------------------------------------------------


@router.post("/checks", status_code=202)
def create_check(request: Request, body: CreateCheckRequest) -> CreateCheckResponse:
    """Create a Check Session and enqueue one message per pending URL.

    Flow (design "Architecture" + Requirements 1.1, 1.2, 1.4, 1.6):

    1. Rate-limit the request per client IP and globally (``checks`` counters).
       A limit hit raises :class:`RateLimitError` → ``429`` with ``Retry-After``.
    2. Parse the submitted lines: mark invalid lines, normalize valid ones, and
       collapse within-batch duplicates. An all-blank submission is a ``422``.
    3. Create the Check Session (``origin="new"``) with every item in its
       initial state (``pending`` / ``invalid`` / ``duplicate_in_batch``).
    4. Enqueue a ``{check_id, item_id}`` message for each ``pending`` item.
    5. Return ``202`` with the per-item initial states.
    """
    settings = get_settings()

    # 1. Rate limit (per-IP + global). Raises RateLimitError (429) on a hit.
    client_ip = get_client_ip(request)
    _enforce_rate_limit(client_ip, settings.rl_checks_per_ip_hour)

    # 2. Parse / normalize / collapse duplicates. One submission string so the
    #    existing parser applies the 10-line limit and item-id numbering.
    raw = "\n".join(body.urls)
    parsed = parse_submission(raw)
    if not parsed:
        raise AppValidationError("No URLs were submitted")

    # 3. Create the Check Session (origin="new") with the parsed initial states.
    check_id = str(uuid.uuid4())
    items = [
        CheckItem(
            # parse_submission uses the same state vocabulary as the session.
            item_id=p.item_id,
            input=p.input,
            state=cast("ItemState", p.state),
            normalized=p.normalized,
        )
        for p in parsed
    ]
    check_session.put_session(check_id, "new", items)

    # 4. Enqueue one message per pending item (IDs only; the worker reads the
    #    rest of the item from the session).
    for p in parsed:
        if p.state == "pending":
            enqueue(settings.check_queue_url, {"check_id": check_id, "item_id": p.item_id})

    # 5. 202 with the per-item initial view.
    return CreateCheckResponse(
        check_id=check_id,
        items=[
            CheckItemView(
                item_id=p.item_id,
                input=p.input,
                normalized=p.normalized,
                state=p.state,
                message=p.message,
            )
            for p in parsed
        ],
    )


# ---------------------------------------------------------------------------
# GET /ingest/checks/{check_id}
# ---------------------------------------------------------------------------


@router.get("/checks/{check_id}")
def get_check(check_id: str) -> dict[str, object]:
    """Return the Check Session for polling, or ``404`` when it has expired.

    The response carries every item's current state, verdict, evidence, final
    URL, hops, and existing-dataset match — the shape the UI polls when the
    real-time ``check.updated`` events are unavailable. The ``#session`` header
    row is never part of the public items view (``get_session`` already excludes
    it), so no check-level internals leak.

    A missing or past-TTL session is reported as ``404 NOT_FOUND`` (Requirement
    4.4 / design: "404 if expired").
    """
    session = check_session.get_session(check_id)
    if session is None:
        raise NotFoundError("Check session not found or expired")

    return {
        "check_id": session.check_id,
        "created_at": session.created_at,
        "origin": session.origin,
        "items": [_item_to_view(item) for item in session.items.values()],
    }


# ---------------------------------------------------------------------------
# POST /ingest/checks/{check_id}/items/{item_id}/retry
# ---------------------------------------------------------------------------


@router.post("/checks/{check_id}/items/{item_id}/retry", status_code=202)
def retry_check_item(request: Request, check_id: str, item_id: str) -> dict[str, str]:
    """Re-queue a single item that ended in ``error`` or timed out.

    - ``429`` when the ``checks`` rate limit is hit (with ``Retry-After``).
    - ``404`` when the session expired or the item is unknown.
    - ``422`` when the item did not end in a retryable state (only an ``error``
      item, or a ``done`` item whose verdict is the ``wont_work`` timeout, may
      be retried — Requirement 3.10).

    The item is reset to ``pending`` (so the worker's conditional claim can take
    it) and a fresh ``{check_id, item_id}`` message is enqueued. Returns ``202``.
    """
    settings = get_settings()

    client_ip = get_client_ip(request)
    _enforce_rate_limit(client_ip, settings.rl_checks_per_ip_hour)

    session = check_session.get_session(check_id)
    if session is None:
        raise NotFoundError("Check session not found or expired")
    item = session.items.get(item_id)
    if item is None:
        raise NotFoundError("Check item not found")

    if not _is_retryable(item):
        raise AppValidationError(
            "This item cannot be retried; only an item that failed or timed out can be retried"
        )

    # Reset to pending so the worker's conditional claim (pending/error) takes
    # it, then enqueue. A timed-out item is `done`, so the generic setter resets
    # its state without an expected_state guard (the user explicitly asked).
    check_session.set_item_fields(check_id, item_id, {"state": "pending"})
    enqueue(settings.check_queue_url, {"check_id": check_id, "item_id": item_id})

    return {"check_id": check_id, "item_id": item_id, "state": "pending"}


# ---------------------------------------------------------------------------
# POST /ingest/checks/{check_id}/add
# ---------------------------------------------------------------------------


@router.post("/checks/{check_id}/add")
def add_check_items(check_id: str, body: AddRequest) -> dict[str, object]:
    """Add the chosen Check items, returning a per-item outcome.

    A thin controller over :func:`app.ingestion.service.add_items`: it maps the
    request body to ``AddRequestItem``\\ s, delegates every per-item decision
    (refuse ``wont_work``, confirm ``limited``, create vs refresh, the ``applied``
    claim, the unique-URL race), and serializes each :class:`~app.ingestion.service.AddResult`
    with ``to_dict()``.

    Always returns ``200`` with ``{results: [{item_id, outcome, dataset_id?,
    message}]}``. An expired or absent Check Session is **not** a ``404``: the
    service already yields outcome ``expired`` for every requested item, and the
    UI uses that to ask the analyst to re-check (Requirement 5.4, design Error
    Handling). ``outcome`` also covers ``created`` / ``refreshed`` /
    ``restored_and_refreshed`` / ``already_refreshing`` (Requirements 6.5, 6.6)
    and ``refused_wont_work`` / ``needs_confirmation``.

    An empty ``items`` list is a client error (``422``) handled by the model's
    ``min_length`` bound. No rate limit is applied — the design's endpoint table
    lists one only for checks, retry, and uploads.
    """
    request_items = [
        AddRequestItem(item_id=it.item_id, confirm_limited=it.confirm_limited) for it in body.items
    ]
    results = service.add_items(check_id, request_items)
    return {"results": [r.to_dict() for r in results]}


# ---------------------------------------------------------------------------
# POST /uploads
# ---------------------------------------------------------------------------


@uploads_router.post("", status_code=201)
def create_upload(request: Request, body: CreateUploadRequest) -> CreateUploadResponse:
    """Issue a pre-signed PUT so the browser can upload a CSV/TSV straight to S3.

    Flow (design "API endpoints" + Error Handling tables, Requirement 7.1):

    1. Rate-limit the request per client IP and globally (``uploads`` counters);
       a limit hit raises :class:`RateLimitError` → ``429`` with ``Retry-After``.
    2. Refuse an over-limit file (``size_bytes`` above ``MAX_UPLOAD_MB``) and a
       file whose extension is not ``.csv``/``.tsv`` with ``422`` — *before* any
       pre-signed URL is issued, so an over-limit file never gets an upload slot.
    3. Mint a random ``upload_id``, build the key via
       :func:`app.storage.keys.upload_file`, and generate a 15-minute pre-signed
       PUT whose signed ``Content-Length`` makes S3 enforce the same byte limit.
    4. Return ``201`` with ``{upload_id, put_url, expires_at}``.

    The uploaded object lands at ``uploads/{upload_id}/file`` and is swept by the
    1-day S3 lifecycle rule if it is never submitted (design Data Models note).
    """
    settings = get_settings()

    # 1. Rate limit (per-IP + global ``uploads``). Raises RateLimitError (429).
    client_ip = get_client_ip(request)
    _enforce_rate_limit(client_ip, settings.rl_uploads_per_ip_hour, action=_UPLOAD_ACTION)

    # 2a. Size: refuse before issuing a URL (design Error Handling table).
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if body.size_bytes > max_bytes:
        raise AppValidationError(
            f"File is larger than the {settings.max_upload_mb} MB upload limit"
        )

    # 2b. Extension: only .csv / .tsv are accepted (Requirement 7.1). The
    #     declared filename is the authoritative gate.
    lower_name = body.filename.lower()
    if not lower_name.endswith(_ALLOWED_UPLOAD_EXTENSIONS):
        raise AppValidationError("Only .csv and .tsv files can be uploaded")

    # 3. Mint the upload id, build the key, and presign a content-length-pinned
    #    PUT for 15 minutes. S3 enforces the exact Content-Length as a second
    #    guard on top of the size check above.
    upload_id = str(uuid.uuid4())
    key = keys.upload_file(upload_id)
    put_url = s3.presign_put(
        key,
        content_type=body.content_type,
        content_length=body.size_bytes,
        expires_in=_UPLOAD_URL_TTL_SECONDS,
    )
    expires_at = (datetime.now(UTC) + timedelta(seconds=_UPLOAD_URL_TTL_SECONDS)).isoformat()

    # 4. 201 with the upload id, the pre-signed URL, and its expiry.
    return CreateUploadResponse(upload_id=upload_id, put_url=put_url, expires_at=expires_at)


# ---------------------------------------------------------------------------
# POST /uploads/{upload_id}/preview
# ---------------------------------------------------------------------------


@uploads_router.post("/{upload_id}/preview")
def preview_upload(upload_id: str) -> dict[str, object]:
    """Preview a staged upload before it is submitted (design "API endpoints").

    Reads the staged ``uploads/{upload_id}/file`` object back from S3 and runs
    :func:`app.ingestion.upload_parser.preview_upload`, returning ``200`` with
    the preview shape ``{columns, suggested_mapping, sample_rows, usable_rows,
    will_keep, keep_rule}`` (Requirements 7.2, 7.6).

    When the staged file is over the size limit, unparseable, or has no usable
    review-text column, the parser deletes the staged object and raises
    :class:`~app.ingestion.upload_parser.UploadInvalidError`; this handler maps
    that to a ``422`` carrying the parser's specific, user-facing message
    (Requirement 7.3, design Error Handling: "Upload invalid at preview → 422
    with reason; the object is deleted"). No rate limit applies — the design's
    endpoint table lists one only for ``POST /uploads``.
    """
    try:
        preview = upload_parser.preview_upload(upload_id)
    except UploadInvalidError as exc:
        # The parser has already deleted the staged object; surface the reason.
        raise AppValidationError(str(exc)) from exc
    return preview.to_dict()


# ---------------------------------------------------------------------------
# POST /datasets/upload
# ---------------------------------------------------------------------------


@datasets_router.post("/upload", status_code=201)
def submit_upload(body: SubmitUploadRequest) -> SubmitUploadResponse:
    """Submit a previewed upload as a new dataset (design "API endpoints").

    A thin controller over :func:`app.ingestion.service.create_from_upload`,
    which owns the work: re-validate the staged file, copy it and write
    ``mapping.json`` under the new dataset, insert the ``datasets`` row
    (``source_type = upload``) and its v1 ``dataset_versions`` row, and enqueue
    processing (Requirement 7.4).

    - A missing/empty ``name`` is a ``422`` from the model's ``min_length``
      bound; a whitespace-only ``name`` is rejected here as well (Requirement
      7.4: the name is required).
    - An invalid staged file makes ``create_from_upload`` raise
      :class:`~app.ingestion.upload_parser.UploadInvalidError` (the staged
      object is deleted); this handler maps that to a ``422`` with the parser's
      message (Requirement 7.3).

    Returns ``201`` with ``{id}`` — the new dataset's id. No rate limit applies
    (the design's endpoint table lists one only for ``POST /uploads``).
    """
    name = body.name.strip()
    if not name:
        raise AppValidationError("A dataset name is required")

    try:
        dataset_id = service.create_from_upload(
            body.upload_id,
            name,
            body.mapping,
            body.description,
        )
    except UploadInvalidError as exc:
        raise AppValidationError(str(exc)) from exc

    return SubmitUploadResponse(id=dataset_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Attribute name used to carry the parsed ``Retry-After`` seconds on a
#: :class:`RateLimitError` so the dedicated handler can emit the header.
_RETRY_AFTER_ATTR = "retry_after_seconds"


def register_rate_limit_header(app: FastAPI) -> None:
    """Install a ``RateLimitError`` handler that emits the ``Retry-After`` header.

    The shared ``AppError`` handler (platform-foundation ``core.errors``) returns
    the ``429`` envelope but cannot know the retry delay. This handler adds the
    numeric ``Retry-After`` header (Requirement 1.6: "say when checks can
    resume") while keeping the same error body, and is registered by the API app
    alongside :func:`app.core.errors.register_error_handlers`.
    """

    @app.exception_handler(RateLimitError)
    async def _handle_rate_limited(request: Request, exc: RateLimitError) -> JSONResponse:
        headers: dict[str, str] = {}
        retry_after = getattr(exc, _RETRY_AFTER_ATTR, None)
        if retry_after is None:
            retry_after = _retry_after_seconds(exc.message)
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=headers,
        )


def _enforce_rate_limit(client_ip: str | None, limit: int, *, action: str = _CHECK_ACTION) -> None:
    """Apply the per-IP + global rate limit for *action*; carry ``Retry-After`` on a 429.

    Re-raises :class:`RateLimitError` (turned into a ``429`` envelope with a
    ``Retry-After`` header by :func:`register_rate_limit_header`) after parsing
    the retry delay from the limiter's message, so the panel can say when the
    action can resume. *action* defaults to the ``checks`` counters; the uploads
    endpoint passes ``uploads`` so the two limits are counted separately.
    """
    try:
        check_rate_limit(action, client_ip, limit)
    except RateLimitError as exc:
        setattr(exc, _RETRY_AFTER_ATTR, _retry_after_seconds(exc.message))
        raise


def _retry_after_seconds(message: str) -> int | None:
    """Pull the integer seconds out of the limiter's ``Retry after N seconds`` text.

    The limiter formats its message as ``"... Retry after {n} seconds."``; this
    extracts ``n`` so the ``429`` carries a numeric ``Retry-After`` header
    without the limiter needing to change its public shape.
    """
    for token in message.replace(".", " ").split():
        if token.isdigit():
            return int(token)
    return None


def _is_retryable_timeout(item: CheckItem) -> bool:
    """True when a ``done`` item's verdict is the ``wont_work`` timeout.

    A timed-out URL finishes as ``done`` with a ``wont_work`` verdict whose
    reason is "Page took too long to load" (Requirement 3.10). Only that done
    case is retryable; a genuine ``wont_work`` (404, blocker, SSRF) is not.
    """
    verdict = item.verdict or {}
    if verdict.get("verdict") != "wont_work":
        return False
    reasons = verdict.get("reasons") or []
    return _TIMEOUT_REASON in reasons


def _is_retryable(item: CheckItem) -> bool:
    """True when *item* may be retried: an ``error`` item or a timed-out one."""
    return item.state == "error" or (item.state == "done" and _is_retryable_timeout(item))


def _item_to_view(item: CheckItem) -> dict[str, object]:
    """Serialize one :class:`CheckItem` for the polling response.

    Mirrors the stored item shape (state, verdict, evidence, final URL, hops,
    existing-dataset match) so the UI reads the same fields whether it learns
    them from a ``check.updated`` event or by polling this endpoint.
    """
    return {
        "item_id": item.item_id,
        "input": item.input,
        "normalized": item.normalized,
        "final_url": item.final_url,
        "state": item.state,
        "hops": item.hops,
        "verdict": item.verdict,
        "existing_dataset": item.existing_dataset,
    }
