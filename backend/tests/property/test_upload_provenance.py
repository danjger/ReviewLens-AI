"""Property-based test for HTML-upload review provenance (task 22.2).

- **Property 9: Upload review provenance.** *For any* uploaded HTML file, every
  verified review the assessment keeps SHALL have text that appears verbatim
  (after whitespace and Unicode normalization) in the uploaded markup; a review
  whose text is not present SHALL be discarded and SHALL NOT count toward the
  verdict. _Validates: Requirements 8.4, 3.3_

The upload assessment path under test is the real one a Check runs for an upload
item (dataset-ingestion design, "capture.from_upload" + viability "assess"):

    capture.from_upload(upload_id, source_url)  # reads the staged file from S3
        -> viability.assess(view, final_url, RobotsResult(allowed=True))

For *any* generated saved page (reusing the URL review-page generator and the
Locator-response generator from task 11's post-processing property), the
assessment runs with an offline, deterministic Locator stub that points at a mix
of real and non-existent refs. The Review Locator only ever points at
*elements*; review text is read from the uploaded page by code in
post-processing and never supplied by the AI (Requirement 8.4 / 3.3), so every
verified review the assessment keeps must have text drawn from the uploaded
markup. The property asserts exactly that: each kept review's text appears
verbatim in the uploaded page's visible text, after the same whitespace and
Unicode normalization applied to both sides.

The set of *verified reviews the assessment keeps* is the first-page
``PageResult`` ``viability.assess`` builds internally (``build_plan``'s second
return value). We capture it by wrapping ``build_plan`` on the viability module
with a recorder that calls the real builder and remembers its result, so the
property sees the complete kept set, not only the two or three samples the
verdict card carries.

S3 is backed by moto (a real in-memory round-trip, no network); the AI client is
the offline ``FakeClaude`` stub via the suite-wide autouse fixture.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from typing import Any

import boto3
import pytest

# Import the capture package first: it fully initializes app.capture (engine +
# upload) and app.ingestion.viability together, avoiding the partial-init
# circular import that starting at app.ingestion.viability would trigger.
from app.capture import from_upload, synthesize_final_url
from app.core.config import get_settings
from app.extraction import plan as plan_mod
from app.extraction.models import ExtractionPlan, PageResult
from app.ingestion import viability
from app.ingestion.robots import RobotsResult
from app.storage import keys
from app.storage import s3 as s3_mod
from hypothesis import given
from hypothesis import strategies as st
from moto import mock_aws
from selectolax.parser import HTMLParser

from tests.property.test_locator_postprocess import (
    _generated_review_page,
    _locator_result_for,
)

_BUCKET = "reviewlens-test"
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Normalize text for the provenance check: Unicode (NFC) + whitespace.

    The design states a kept review's text must appear verbatim "after
    whitespace and Unicode normalization". We apply NFC Unicode normalization
    (so canonically-equivalent sequences compare equal) and then collapse every
    run of whitespace to a single space and strip — the same whitespace rule the
    cleaner and post-processing use (``cleaner._WS_RE``). Both the review text
    and the page's visible text go through this before the containment check.
    """
    return _WS_RE.sub(" ", unicodedata.normalize("NFC", text)).strip()


@pytest.fixture()
def s3_bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A moto-backed S3 bucket so ``from_upload`` reads a real staged object."""
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


def _install_offline_locator(
    monkeypatch: pytest.MonkeyPatch, data: st.DataObject, rating_scale: int
) -> None:
    """Replace the Locator AI call with an offline stub over the real page refs.

    Mirrors task 11's post-processing property: the stub points at a mix of real
    refs (so some reviews survive) and non-existent refs (which post-processing
    must discard), exercising the keep/discard logic the provenance property
    depends on — all offline and deterministic.
    """

    def _fake_locate(page: Any, *, url: str, title: str) -> Any:  # noqa: ARG001
        return data.draw(_locator_result_for(page.lookup, rating_scale=rating_scale))

    monkeypatch.setattr(plan_mod.locator, "locate", _fake_locate, raising=True)


def _install_plan_recorder(monkeypatch: pytest.MonkeyPatch) -> list[PageResult]:
    """Record the first-page ``PageResult`` ``assess`` builds via ``build_plan``.

    ``viability.assess`` returns only ``(verdict, plan)``; the kept reviews live
    on the first-page ``PageResult`` ``build_plan`` returns alongside the plan.
    We wrap ``viability.build_plan`` so the real builder runs and its page result
    is captured, giving the property the complete set of verified reviews the
    assessment kept (not just the verdict's two or three samples).
    """
    captured: list[PageResult] = []
    real_build_plan = viability.build_plan

    def _recording_build_plan(html: str, url: str, title: str) -> tuple[ExtractionPlan, PageResult]:
        plan, page = real_build_plan(html, url, title)
        captured.append(page)
        return plan, page

    monkeypatch.setattr(viability, "build_plan", _recording_build_plan, raising=True)
    return captured


@given(data=st.data())
def test_upload_review_provenance(
    data: st.DataObject,
    s3_bucket: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 9: Upload review provenance.

    For any uploaded HTML file, every verified review the assessment keeps has
    text appearing verbatim (after whitespace and Unicode normalization) in the
    uploaded markup. The Review Locator only points at elements and never
    supplies text (Requirement 8.4 / 3.3), so verification against the uploaded
    page text holds for every kept review.
    Validates: Requirements 8.4, 3.3
    """
    html, scale = data.draw(_generated_review_page())
    _install_offline_locator(monkeypatch, data, scale)
    captured = _install_plan_recorder(monkeypatch)

    # Stage the generated saved page as an upload (real moto S3 round-trip).
    upload_id = "prop9"
    s3_bucket.put_object(Bucket=_BUCKET, Key=keys.upload_file(upload_id), Body=html.encode("utf-8"))

    view = from_upload(upload_id, source_url=None)
    final_url = synthesize_final_url(upload_id, None)
    verdict, plan = viability.assess(view, final_url, RobotsResult(allowed=True))

    # The uploaded page's normalized visible text — the only legitimate source
    # of any kept review's text (collapsing a substring yields a substring of
    # the collapsed whole, so a review read from any element appears here).
    tree = HTMLParser(view.html)
    page_text = _normalize(tree.body.text()) if tree.body is not None else ""

    # The verified reviews the assessment kept. When the free pre-scan finds a
    # certain blocker (e.g. a tiny page is an empty shell), ``assess`` stops
    # before ``build_plan`` and keeps no reviews — the property then holds
    # vacuously (there is nothing to have invented), and the blocked verdict
    # carries no samples either.
    kept = captured[-1].reviews if captured else []

    for review in kept:
        assert _normalize(review.text) in page_text, (
            f"kept review text {review.text!r} does not appear verbatim in the uploaded markup"
        )

    # Provenance also holds for the samples the verdict surfaces, and the kept
    # count the verdict reports matches the reviews actually read from the page.
    for sample in verdict.evidence.samples:
        assert _normalize(sample.text) in page_text
    assert verdict.evidence.reviews_verified == len(kept)
