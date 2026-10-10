"""Page capture for ReviewLens AI.

Two capture sources live here:

- :func:`render` (``app.capture.engine``) renders user-supplied URLs in headless
  Chromium during the Check step. It needs the Chromium browser the **workers
  image** installs; the API and chat services never call it.
- :func:`from_upload` (``app.capture.upload``) turns an analyst-uploaded saved
  HTML page into the same :class:`~app.ingestion.viability.CaptureView` a live
  render produces, doing **no browser or network work**. It therefore belongs in
  the **base image** and is importable everywhere.

Public surface::

    from app.capture import render, CaptureResult, from_upload

    result = render(final_url, prefix="checks/{check_id}/{item_id}/")
    view = from_upload(upload_id, source_url)
"""

from app.capture.engine import (
    CaptureBlockedError,
    CaptureResult,
    render,
    shutdown_browser,
)
from app.capture.upload import (
    UploadNotReadableError,
    from_upload,
    synthesize_final_url,
)

__all__ = [
    "CaptureBlockedError",
    "CaptureResult",
    "UploadNotReadableError",
    "from_upload",
    "render",
    "shutdown_browser",
    "synthesize_final_url",
]
