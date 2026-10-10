"""Upload-backed capture (``from_upload``) — base image, no browser.

``from_upload(upload_id, source_url)`` turns an analyst-uploaded saved page into
the same :class:`~app.ingestion.viability.CaptureView` a live render produces,
so :func:`app.ingestion.viability.assess` runs on an upload **unchanged**
(dataset-ingestion design, ``capture.from_upload(upload_id, source_url) ->
CaptureView``). Unlike :func:`app.capture.engine.render`, this does **no browser
or network work**: it only reads the staged file from S3 and decodes it, which
is why it lives in the base image rather than the workers image.

What it does (Requirements 8.2, 8.3, 8.5, 4.3):

- Reads ``uploads/{upload_id}/file`` from S3 (key from
  :func:`app.storage.keys.upload_file`) and decodes it as UTF-8 text. A file
  that is not readable text is **rejected** with a specific, user-facing message
  and the staged object is **deleted**, mirroring the bad-CSV handling in
  :func:`app.ingestion.upload_parser.preview_upload` (Requirement 7.3 / 8.2).
- Parses the ``<title>`` from the markup into ``page_title`` (Requirement 4.3).
- Synthesizes the two fields a live HTTP render would have supplied, because an
  uploaded file has no HTTP response:
    - ``final_url`` = the analyst-supplied *source_url* when present, otherwise
      the synthetic placeholder ``upload://{upload_id}``. It is used only as
      plan context and is **never fetched** (Requirements 8.5, 8.8).
    - ``main_status`` = ``200`` (synthetic): an uploaded page the analyst already
      viewed is treated as a successful render, so the "main status not 200 →
      ``wont_work``" rule (Requirement 2.7) never misfires on an upload.
- Returns the uploaded markup as the view's ``html`` and performs **no
  sub-resource fetch** of any kind.

Engineering rules honoured here:

- **No browser, no network.** Only S3 reads/deletes; nothing is rendered and no
  link, script, image, or iframe in the uploaded page is fetched (Requirement
  8.5).
- **Keys.** The object key comes only from :mod:`app.storage.keys`.
- **Stateless.** No process memory or local disk is relied on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from selectolax.parser import HTMLParser

from app.storage import keys, s3

if TYPE_CHECKING:
    # Imported lazily inside ``from_upload`` at runtime to avoid a circular
    # import: ``app.ingestion.viability`` imports ``app.capture.engine`` (which
    # eagerly loads this module via ``app.capture.__init__``), and this module
    # needs ``CaptureView`` from ``viability`` — importing it at module load
    # would require ``viability`` to be fully initialized first. The annotation
    # is a string under ``from __future__ import annotations``, so TYPE_CHECKING
    # covers typing; the function does the real import when it runs.
    from app.ingestion.viability import CaptureView

#: Synthetic HTTP status for an uploaded page (see module docstring / design).
_SYNTHETIC_MAIN_STATUS = 200


class UploadNotReadableError(Exception):
    """An uploaded file is not readable text, so it cannot become a capture.

    Raised by :func:`from_upload` when the staged object cannot be decoded as
    UTF-8 text. The ``message`` is user-facing and mirrors the bad-CSV wording
    of :class:`app.ingestion.upload_parser.UploadInvalidError`; the staged S3
    object is **deleted** before this propagates, so an unreadable upload never
    leaves an orphan behind (Requirement 8.2 / 7.3).
    """


def synthesize_final_url(upload_id: str, source_url: str | None) -> str:
    """Return the synthetic ``final_url`` for an upload (design: ``from_upload``).

    An uploaded file has no HTTP response, so the Viability assessment needs a
    stand-in ``final_url`` for plan context only — it is **never fetched**
    (Requirements 8.5, 8.8). It is the analyst-supplied *source_url* when
    present, otherwise the synthetic placeholder ``upload://{upload_id}``.

    Kept as a small helper so the one place that invents the placeholder is the
    same place :func:`from_upload` documents, and the check handler (task 18)
    derives the exact same value to pass alongside the returned
    :class:`CaptureView` into :func:`app.ingestion.viability.assess`.
    """
    return source_url if source_url else f"upload://{upload_id}"


def _parse_title(html: str) -> str:
    """Return the uploaded page's ``<title>`` text, or ``""`` when absent.

    Reads the title by code with selectolax (the same parser the Extraction
    Engine uses), whitespace-normalized. A page with no ``<title>`` yields the
    empty string, matching :class:`~app.capture.engine.CaptureResult`'s
    ``page_title`` contract (Requirement 4.3).
    """
    node = HTMLParser(html).css_first("title")
    if node is None:
        return ""
    return " ".join(node.text().split())


def from_upload(upload_id: str, source_url: str | None = None) -> CaptureView:
    """Build a :class:`CaptureView` from an uploaded saved HTML page.

    Reads ``uploads/{upload_id}/file`` from S3, decodes it as text, parses the
    ``<title>``, and returns the existing :class:`CaptureView` with the uploaded
    markup as ``html`` and synthetic ``final_url`` / ``main_status`` (see the
    module docstring). No browser is launched and no sub-resource is fetched
    (Requirements 8.2, 8.3, 8.5, 4.3).

    Args:
        upload_id: The id of the staged upload (``POST /uploads``), identifying
            ``uploads/{upload_id}/file``.
        source_url: The analyst-supplied original page URL, used only as plan
            context for ``final_url``; never fetched. When ``None`` or empty, a
            synthetic ``upload://{upload_id}`` placeholder is used instead
            (Requirements 8.5, 8.8).

    Returns:
        A :class:`CaptureView` ready to pass to
        :func:`app.ingestion.viability.assess`.

    Raises:
        UploadNotReadableError: The staged file cannot be decoded as UTF-8 text.
            The staged S3 object has been deleted before this is raised
            (Requirement 8.2).
    """
    # Local import avoids the module-load circular import documented above.
    from app.ingestion.viability import CaptureView

    key = keys.upload_file(upload_id)
    raw = s3.get_bytes(key)

    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Reject → delete the staged object so no orphan is left, mirroring the
        # bad-CSV handling (Requirement 8.2 / 7.3).
        s3.delete_object(key)
        raise UploadNotReadableError(
            "The file could not be read as text. Upload a saved HTML page (.html, .htm, or .mhtml)."
        ) from exc

    return CaptureView(
        html=html,
        page_title=_parse_title(html),
        main_status=_SYNTHETIC_MAIN_STATUS,
    )
