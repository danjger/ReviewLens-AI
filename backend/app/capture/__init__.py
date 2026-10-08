"""Headless-browser page capture for ReviewLens AI (workers image only).

This package renders user-supplied URLs in headless Chromium during the Check
step. It is only available in the workers image (which installs Playwright and
Chromium); the API and chat services never import it.

Public surface::

    from app.capture import render, CaptureResult

    result = render(final_url, prefix="checks/{check_id}/{item_id}/")
"""

from app.capture.engine import (
    CaptureBlockedError,
    CaptureResult,
    render,
    shutdown_browser,
)

__all__ = [
    "CaptureBlockedError",
    "CaptureResult",
    "render",
    "shutdown_browser",
]
