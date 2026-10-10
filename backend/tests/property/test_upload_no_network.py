"""Property-based test for the HTML-upload no-network guarantee (task 22.1).

- **Property 8: Uploaded pages are never fetched.** *For any* uploaded HTML
  file, assessing it SHALL make no outbound network request for the page or any
  of its links, scripts, images, or iframes; the assessment reads only the
  saved markup. _Validates: Requirements 8.5, 8.6_

The upload assessment path under test is the real one a Check runs for an upload
item (dataset-ingestion design, "capture.from_upload" + viability "assess"):

    capture.from_upload(upload_id, source_url)  # reads the staged file from S3
        -> viability.assess(view, final_url, RobotsResult(allowed=True))

For *any* generated saved page (reusing the URL review-page generator from task
11's post-processing property), the whole path is driven with a **network
guard** installed over every way the ingestion code could reach the network —
the reachability probe, the robots.txt check, the headless-browser render, and
the low-level ``httpx`` request/send transport. Each guard raises
``_NetworkTouchedError`` if it is ever called, so the property fails loudly the
moment any outbound fetch is attempted. The guards sit on the exact seams the
URL path uses (``probe``, ``robots.check``, ``capture.render``) so this proves
the upload path performs none of that work, not merely that one function was
skipped.

The Review Locator (the one AI call ``build_plan`` would make) is replaced with
an offline, deterministic stub that points only at real refs of the uploaded
page, so the assessment runs fully offline: the property is about *no network*
and provenance, not live AI quality (and the AI client itself is already the
offline ``FakeClaude`` stub via the suite-wide autouse fixture).

S3 is backed by moto, so ``from_upload``'s staged-object read is a real
in-memory S3 round-trip with no network, mirroring task 17.1's unit tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import httpx
import pytest

# Import the capture package first: it fully initializes app.capture (engine +
# upload) and app.ingestion.viability together, avoiding the partial-init
# circular import that starting at app.ingestion.viability would trigger.
from app.capture import from_upload, synthesize_final_url
from app.core.config import get_settings
from app.extraction import plan as plan_mod
from app.ingestion import viability
from app.ingestion.robots import RobotsResult
from app.storage import keys
from app.storage import s3 as s3_mod
from hypothesis import given
from hypothesis import strategies as st
from moto import mock_aws

from tests.property.test_locator_postprocess import (
    _generated_review_page,
    _locator_result_for,
)

_BUCKET = "reviewlens-test"


class _NetworkTouchedError(RuntimeError):
    """Raised by a network guard if the upload path ever tries to go online."""


@pytest.fixture()
def s3_bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A moto-backed S3 bucket so ``from_upload`` reads a real staged object.

    Mirrors task 17.1's ``from_upload`` unit-test fixture: moto gives an
    in-memory S3 (no network) and the settings/client caches are cleared on the
    way in and out so the bucket name resolves to the test bucket.
    """
    monkeypatch.setenv("S3_BUCKET", _BUCKET)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "")
    get_settings.cache_clear()
    s3_mod.reset_client()
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET)
        yield client
    get_settings.cache_clear()
    s3_mod.reset_client()


@pytest.fixture()
def network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install guards that raise if the upload path makes any outbound fetch.

    Covers every seam the URL Check uses to reach the network, so an upload
    assessment that stays offline passes and one that fetches anything fails:

    - ``robots.check`` — the URL path's robots.txt fetch;
    - ``capture.render`` — the headless-browser render (and its sub-resources);
    - ``httpx.Client.send`` / ``httpx.Client.request`` — the low-level transport
      every HTTP client in the app funnels through (the reachability probe, the
      robots fetch, every browser sub-request), so even an un-mocked helper that
      tried to reach the page or a sub-resource would trip the guard.
    """

    def _boom(*_args: object, **_kwargs: object) -> Any:
        raise _NetworkTouchedError("the upload assessment attempted an outbound network fetch")

    # The URL path's HTTP seams: the reachability probe/robots check and the
    # headless-browser render. Patch them at their source modules so any call
    # from anywhere in the assessment trips the guard.
    import app.capture.engine as capture_engine
    import app.ingestion.robots as robots_mod

    monkeypatch.setattr(robots_mod, "check", _boom, raising=True)
    monkeypatch.setattr(capture_engine, "render", _boom, raising=True)

    # Low-level transport: nothing in the upload path should ever issue a request.
    monkeypatch.setattr(httpx.Client, "send", _boom, raising=True)
    monkeypatch.setattr(httpx.Client, "request", _boom, raising=True)


def _install_offline_locator(
    monkeypatch: pytest.MonkeyPatch, data: st.DataObject, rating_scale: int
) -> None:
    """Replace the Locator AI call with an offline stub over the real page refs.

    ``viability.assess`` calls ``extraction.build_plan`` which calls
    ``locator.locate`` (the single AI call). We swap ``plan.locator.locate`` for
    a function that builds a :class:`LocatorResult` from task 11's generator,
    pointing only at refs that exist in the cleaned page passed to it, so
    post-processing runs on genuine elements and the whole path is deterministic
    and network-free.
    """

    def _fake_locate(page: Any, *, url: str, title: str) -> Any:  # noqa: ARG001
        return data.draw(_locator_result_for(page.lookup, rating_scale=rating_scale))

    monkeypatch.setattr(plan_mod.locator, "locate", _fake_locate, raising=True)


@given(data=st.data())
def test_uploaded_pages_are_never_fetched(
    data: st.DataObject,
    s3_bucket: Any,
    network_guard: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 8: Uploaded pages are never fetched.

    For any uploaded HTML file, assessing it (``capture.from_upload`` then
    ``viability.assess``) makes no outbound network request for the page or any
    sub-resource. Asserted with a network guard over the probe, robots check,
    browser render, and the httpx transport: any outbound fetch raises and fails
    the property.
    Validates: Requirements 8.5, 8.6
    """
    html, scale = data.draw(_generated_review_page())
    _install_offline_locator(monkeypatch, data, scale)

    # Stage the generated saved page as an upload (real moto S3 round-trip).
    upload_id = "prop8"
    s3_bucket.put_object(Bucket=_BUCKET, Key=keys.upload_file(upload_id), Body=html.encode("utf-8"))

    # The upload path under test: build the capture, then assess with a
    # no-restriction robots result (as the check handler's upload branch does).
    view = from_upload(upload_id, source_url=None)
    final_url = synthesize_final_url(upload_id, None)

    # No guard fires: the whole assessment reads only the saved markup.
    verdict, plan = viability.assess(view, final_url, RobotsResult(allowed=True))

    # The assessment produced a result from the saved markup alone.
    assert verdict.verdict in {"will_work", "limited", "wont_work"}
    assert plan is not None
    # The capture carried the uploaded markup through verbatim and invented no
    # HTTP response (synthetic 200), confirming nothing was fetched to build it.
    assert view.html == html
    assert view.main_status == 200
