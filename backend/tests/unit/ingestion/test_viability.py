"""Unit tests for ``app.ingestion.viability.assess`` (dataset-ingestion task 3.2).

These are *orchestration* tests: they verify the wiring of the viability
pipeline, not the detailed verdict rules (task 3.3) or the AI-unavailable
fallback phrasing (task 3.4).

Two things are checked:

- **Pre-scan short-circuit:** a certain rule-based blocker (empty shell,
  challenge, login wall) yields a ``wont_work`` verdict, a minimal blocked
  plan, and crucially makes **no AI call** — here guaranteed by stubbing
  ``build_plan`` with a spy that fails the test if it is ever called
  (Requirement 3.2 / 3.5).
- **Happy path:** a page that passes the pre-scan calls ``build_plan`` once and
  returns that plan plus a complete verdict whose evidence reflects the
  first-page result (Requirement 3.1, 3.3, 3.11).

``build_plan`` is stubbed at the ``viability`` module boundary so the tests are
fast and need no AI fixtures; the Extraction Engine's own behaviour is covered
by ``tests/unit/extraction``.
"""

from __future__ import annotations

import pytest
from app.core.config import get_settings
from app.extraction.models import (
    ExtractionPlan,
    FirstPageStats,
    LocatorSelectors,
    NextPage,
    NextPageRule,
    PageResult,
    VerifiedReview,
)
from app.ingestion import viability
from app.ingestion.robots import RobotsResult
from app.ingestion.viability import CaptureView, assess


@pytest.fixture(autouse=True)
def _fresh_settings() -> None:
    """Reset memoised settings so default thresholds are used."""
    get_settings.cache_clear()


_FINAL_URL = "https://example.com/reviews"
_ALLOWED = RobotsResult(allowed=True)


def _content_page(review_count: int = 24) -> str:
    """Return a content-rich review page that passes the pre-scan."""
    reviews = "".join(
        f"<article><p>Review number {i}: this product worked well for our team "
        f"and support was responsive throughout onboarding.</p></article>"
        for i in range(review_count)
    )
    return f"<html><body><main><h1>Acme CRM Reviews</h1>{reviews}</main></body></html>"


def _make_plan(
    *,
    method: str = "selectors",
    verified: int,
    discarded: int = 0,
    next_rule: str = "selector",
    reported_total: int | None = None,
    confidence: str | None = "high",
    degraded: bool = False,
) -> ExtractionPlan:
    return ExtractionPlan(
        version=1,
        created_at="2024-01-02T03:04:05+00:00",
        locator_model="model-x",
        prompt_version="locator_v1",
        method=method,  # type: ignore[arg-type]
        selectors=LocatorSelectors(item=".r") if method == "selectors" else LocatorSelectors(),
        rating_scale=5.0,
        next_page_rule=NextPageRule(
            type=next_rule,  # type: ignore[arg-type]
            css=".next" if next_rule == "selector" else None,
        ),
        first_page=FirstPageStats(
            verified=verified,
            discarded=discarded,
            structured_count=0,
            per_page_rate=verified,
        ),
        reported_total=reported_total,
        entity_hint="Acme CRM",
        confidence=confidence,  # type: ignore[arg-type]
        degraded=degraded,
    )


def _make_page_result(
    *,
    verified: int,
    method: str = "selectors",
    next_url: str | None = "https://example.com/reviews?page=2",
    reported_total: int | None = None,
) -> PageResult:
    reviews = [
        VerifiedReview(text=f"Review number {i}: this product worked well", rating=5.0, date=None)
        for i in range(verified)
    ]
    return PageResult(
        reviews=reviews,
        method_used=method,  # type: ignore[arg-type]
        fallback=False,
        discarded={},
        structured_agreement=None,
        next_page=NextPage(url=next_url, rule_used="plan_rule", reason_if_none=None),
        blocker=None,
        reported_total=reported_total,
    )


# ---------------------------------------------------------------------------
# Pre-scan short-circuit → wont_work with NO AI call
# ---------------------------------------------------------------------------


class TestPrescanShortCircuit:
    def _no_ai(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("build_plan must not be called when the pre-scan blocks the page")

    def test_empty_shell_blocks_without_ai(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(viability, "build_plan", self._no_ai)
        capture = CaptureView(html="<html><body></body></html>", page_title="x", main_status=200)

        verdict, plan = assess(capture, _FINAL_URL, _ALLOWED)

        assert verdict.verdict == "wont_work"
        assert verdict.evidence.blocker == "empty"
        assert verdict.evidence.reviews_verified == 0
        assert plan.degraded is True
        assert plan.next_page_rule.type == "none"

    def test_challenge_blocks_without_ai(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(viability, "build_plan", self._no_ai)
        html = (
            '<html><body><div class="g-recaptcha"></div>'
            "<p>Please verify you are a human.</p></body></html>"
        )
        capture = CaptureView(html=html, page_title="Just a moment", main_status=200)

        verdict, _ = assess(capture, _FINAL_URL, _ALLOWED)

        assert verdict.verdict == "wont_work"
        assert verdict.evidence.blocker == "captcha"

    def test_login_wall_blocks_without_ai(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(viability, "build_plan", self._no_ai)
        html = (
            "<html><body><h1>Members only</h1>"
            '<form><input type="password" name="pw"></form>'
            "<p>Please log in to continue.</p></body></html>"
        )
        capture = CaptureView(html=html, page_title="Sign in", main_status=200)

        verdict, _ = assess(capture, _FINAL_URL, _ALLOWED)

        assert verdict.verdict == "wont_work"
        assert verdict.evidence.blocker == "login_wall"

    def test_blocked_evidence_carries_capture_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(viability, "build_plan", self._no_ai)
        capture = CaptureView(html="", page_title="Loading", main_status=200)

        verdict, _ = assess(capture, _FINAL_URL, _ALLOWED)

        assert verdict.evidence.page_title == "Loading"
        assert verdict.evidence.main_status == 200


# ---------------------------------------------------------------------------
# Happy path → build_plan called once, plan + verdict returned
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_passes_page_to_build_plan_and_returns_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _make_plan(verified=24, reported_total=1540)
        page = _make_page_result(verified=24, reported_total=1540)
        calls: list[tuple[str, str, str]] = []

        def _fake_build_plan(html: str, url: str, title: str) -> tuple[ExtractionPlan, PageResult]:
            calls.append((html, url, title))
            return plan, page

        monkeypatch.setattr(viability, "build_plan", _fake_build_plan)
        capture = CaptureView(html=_content_page(), page_title="Acme CRM Reviews", main_status=200)

        verdict, returned_plan = assess(capture, _FINAL_URL, _ALLOWED)

        assert len(calls) == 1
        assert calls[0][1] == _FINAL_URL
        assert calls[0][2] == "Acme CRM Reviews"
        assert returned_plan is plan
        assert verdict.verdict == "will_work"
        assert verdict.evidence.reviews_verified == 24
        assert verdict.evidence.method == "selectors"
        assert verdict.evidence.pagination is True
        assert verdict.evidence.reported_total == 1540
        assert verdict.evidence.page_title == "Acme CRM Reviews"
        assert verdict.evidence.main_status == 200
        assert 1 <= len(verdict.evidence.samples) <= 3

    def test_blocker_in_plan_result_gives_wont_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = _make_plan(verified=0, method="ai_direct", next_rule="none")
        page = _make_page_result(verified=0, method="ai_direct", next_url=None)
        page = page.model_copy(update={"blocker": "consent_wall"})

        monkeypatch.setattr(viability, "build_plan", lambda *a, **k: (plan, page))
        capture = CaptureView(html=_content_page(), page_title="t", main_status=200)

        verdict, _ = assess(capture, _FINAL_URL, _ALLOWED)

        assert verdict.verdict == "wont_work"
        assert verdict.evidence.blocker == "consent_wall"

    def test_few_reviews_gives_limited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = _make_plan(verified=2, next_rule="none")
        page = _make_page_result(verified=2, next_url=None)

        monkeypatch.setattr(viability, "build_plan", lambda *a, **k: (plan, page))
        capture = CaptureView(html=_content_page(), page_title="t", main_status=200)

        verdict, _ = assess(capture, _FINAL_URL, _ALLOWED)

        assert verdict.verdict == "limited"
        assert verdict.evidence.reviews_verified == 2

    def test_degraded_plan_capped_at_limited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # AI unavailable: build_plan returns a degraded structured-only plan.
        plan = _make_plan(
            verified=12, method="structured", next_rule="none", degraded=True, confidence=None
        )
        page = _make_page_result(verified=12, method="structured", next_url=None)

        monkeypatch.setattr(viability, "build_plan", lambda *a, **k: (plan, page))
        capture = CaptureView(html=_content_page(), page_title="t", main_status=200)

        verdict, _ = assess(capture, _FINAL_URL, _ALLOWED)

        assert verdict.verdict == "limited"

    def test_robots_disallow_adds_warning_without_changing_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _make_plan(verified=24, reported_total=30)
        page = _make_page_result(verified=24, reported_total=30)
        monkeypatch.setattr(viability, "build_plan", lambda *a, **k: (plan, page))

        robots = RobotsResult(allowed=False, warning="disallowed by robots.txt")
        capture = CaptureView(html=_content_page(), page_title="t", main_status=200)

        verdict, _ = assess(capture, _FINAL_URL, robots)

        assert verdict.verdict == "will_work"
        assert verdict.warnings == ["disallowed by robots.txt"]
