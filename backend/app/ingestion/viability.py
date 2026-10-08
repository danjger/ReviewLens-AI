"""Viability assessment orchestration (``assess``).

``assess(capture, final_url, robots)`` is the Viability Check: it turns a
rendered page into a :class:`~app.ingestion.verdict.Verdict` and an
:class:`~app.extraction.models.ExtractionPlan` (dataset-ingestion design,
"Viability assessment (AI-first)"). The check handler (task 4.2) calls it after
capture; Add stores the plan with the capture and the verdict under
``status_detail.viability`` (Requirement 3.11).

This module only *orchestrates*. Each heavy step lives elsewhere and is reused,
never re-implemented:

1. **Rule-based pre-scan (free)** — :func:`app.ingestion.prescan.prescan`
   (task 3.1). A certain blocker stops here with ``wont_work`` and **no AI
   call** (design step 1 / Requirement 3.2, 3.5).
2. **Steps 2–6 (structured data, Review Locator, verification, selector
   validation, method choice)** — a single call to
   :func:`app.extraction.build_plan` (``review-extraction``). When the AI is
   unavailable it returns a degraded structured-only plan rather than raising,
   so the fallback is handled inside the Extraction Engine and surfaced here as
   ``plan.degraded`` (Requirement 3.13; the verdict capping is task 3.4).
3. **Verdict** — :func:`app.ingestion.verdict.build_verdict`, the seam task 3.3
   (full rules, phrasing, samples) and task 3.4 (AI-unavailable capping) refine.

Interface note (keeps the design signature and stays testable without S3):
``assess`` takes a lightweight :class:`CaptureView` — the rendered ``html``, the
page ``title``, and the browser's ``main_status`` — which the check handler
builds from a :class:`~app.capture.engine.CaptureResult` plus the HTML it reads
back from S3 (:func:`CaptureView.from_capture`). Tests pass a ``CaptureView``
directly with inline HTML, so no S3 is needed to exercise the orchestration.

Stateless and side-effect free: ``assess`` makes no writes and keeps no state;
the only outbound work is the AI call inside ``build_plan`` (through the
instrumented client) and the robots fetch the caller already performed.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.capture.engine import CaptureResult
from app.core.config import get_settings
from app.extraction import build_plan
from app.extraction.models import (
    ExtractionPlan,
    FirstPageStats,
    LocatorSelectors,
    NextPage,
    NextPageRule,
    PageResult,
)
from app.ingestion import prescan
from app.ingestion.robots import RobotsResult
from app.ingestion.verdict import Verdict, blocked_verdict, build_verdict

# ---------------------------------------------------------------------------
# Capture view (testable input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureView:
    """The rendered-page inputs ``assess`` needs, independent of S3.

    The check handler builds one from the :class:`CaptureResult` returned by
    ``capture.render()`` plus the HTML it reads back from S3; tests build one
    directly from an inline HTML string. Keeping the HTML on this value object
    means ``assess`` never has to touch S3, so it stays unit-testable.

    Attributes:
        html: The rendered HTML of the page.
        page_title: The page ``<title>`` (Requirement 4.3).
        main_status: The main document's HTTP status in the browser, or
            ``None`` when none was observed.
    """

    html: str
    page_title: str = ""
    main_status: int | None = None

    @classmethod
    def from_capture(cls, capture: CaptureResult, html: str) -> CaptureView:
        """Build a view from a :class:`CaptureResult` and its rendered HTML.

        The HTML is passed in (the handler reads it back from
        ``capture.html_key`` in S3) rather than stored on the capture result,
        so this module needs no S3 access.
        """
        return cls(
            html=html,
            page_title=capture.page_title,
            main_status=capture.main_status,
        )


# ---------------------------------------------------------------------------
# Blocked-plan placeholder (pre-scan short-circuit)
# ---------------------------------------------------------------------------

#: A reason, keyed by the pre-scan blocker, shown on the ``wont_work`` card.
#: Task 3.3 owns the final phrasing; these are clear plain-language defaults.
_BLOCKER_REASONS: dict[str, str] = {
    prescan.Blocker.EMPTY_SHELL: "The page loaded no readable content",
    prescan.Blocker.CHALLENGE: "The page is a bot or CAPTCHA challenge",
    prescan.Blocker.LOGIN_WALL: "The page requires signing in to see reviews",
}


def _blocked_plan(blocker: prescan.Blocker, created_at: str) -> ExtractionPlan:
    """Return a minimal Extraction Plan for a pre-scan-blocked page.

    A blocked page never reaches the Extraction Engine, so there is no real
    plan. The caller still returns a plan for a consistent interface; the
    minimal representation mirrors the degraded path in ``extraction/plan.py``:
    ``method="structured"``, no selectors, an unknown rating scale, a
    ``type="none"`` next-page rule, zeroed first-page stats, and
    ``degraded=True`` (nothing was read). The blocker itself is recorded on the
    accompanying :class:`PageResult`.
    """
    return ExtractionPlan(
        version=1,
        created_at=created_at,
        locator_model=None,
        prompt_version=None,
        method="structured",
        selectors=LocatorSelectors(),
        rating_scale=None,
        next_page_rule=NextPageRule(type="none"),
        first_page=FirstPageStats(),
        reported_total=None,
        entity_hint=None,
        confidence=None,
        degraded=True,
    )


def _blocked_page_result(blocker: prescan.Blocker) -> PageResult:
    """Return the first-page result for a pre-scan-blocked page.

    Carries the recognised ``blocker`` as one of the Extraction Engine blocker
    labels so the evidence object records it. The pre-scan's ``empty_shell`` maps
    to the engine's ``empty`` blocker; challenge/login map to ``captcha`` /
    ``login_wall`` respectively.
    """
    engine_blocker = _PRESCAN_TO_ENGINE_BLOCKER[blocker]
    return PageResult(
        reviews=[],
        method_used="structured",
        fallback=False,
        discarded={},
        structured_agreement=None,
        next_page=NextPage(url=None, rule_used="none", reason_if_none="blocked"),
        blocker=engine_blocker,
        reported_total=None,
    )


#: Map the pre-scan's blocker labels to the Extraction Engine ``Blocker``
#: literals used in :class:`PageResult`/evidence.
_PRESCAN_TO_ENGINE_BLOCKER: dict[prescan.Blocker, str] = {
    prescan.Blocker.EMPTY_SHELL: "empty",
    prescan.Blocker.CHALLENGE: "captcha",
    prescan.Blocker.LOGIN_WALL: "login_wall",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def assess(
    capture: CaptureView,
    final_url: str,
    robots: RobotsResult,
) -> tuple[Verdict, ExtractionPlan]:
    """Assess whether reviews can be extracted from a captured page.

    Orchestration order (design "Viability assessment (AI-first)"):

    1. Run the free rule-based pre-scan. If it finds a *certain* blocker, stop
       with a ``wont_work`` verdict and **no AI call**, returning a minimal
       blocked plan.
    2. Otherwise call :func:`app.extraction.build_plan` for steps 2–6 (structured
       data, Review Locator, verification, selector validation, method choice).
       It returns a degraded structured-only plan if the AI is unavailable.
    3. Build the verdict from the plan + first-page result, attaching the
       robots warning (which never changes the label on its own, Requirement
       3.12).

    Args:
        capture: The rendered page (HTML, title, main status).
        final_url: The URL that returned HTTP 200 (used for plan context).
        robots: The robots.txt outcome from :func:`app.ingestion.robots.check`.

    Returns:
        ``(verdict, plan)``. The plan is always returned (a minimal blocked plan
        on the pre-scan short-circuit) so the caller has a consistent interface
        and can save it with the capture (Requirement 3.11).
    """
    settings = get_settings()
    robots_warning = None if robots.allowed else robots.warning

    # Step 1: free rule-based pre-scan. A certain blocker stops here, no AI.
    blocker = prescan.prescan(capture.html)
    if blocker is not None:
        page_result = _blocked_page_result(blocker)
        plan = _blocked_plan(blocker, created_at=_now_iso())
        verdict = blocked_verdict(
            plan,
            page_result,
            blocker=_PRESCAN_TO_ENGINE_BLOCKER[blocker],
            reason=_BLOCKER_REASONS[blocker],
            page_title=capture.page_title,
            main_status=capture.main_status,
        )
        return verdict, plan

    # Steps 2–6: structured data, Locator, verification, selectors, method.
    # build_plan degrades to a structured-only plan when the AI is unavailable
    # (plan.degraded == True); it does not raise for that case.
    plan, first_page = build_plan(capture.html, final_url, capture.page_title)

    # Step 7: verdict (full rules owned by task 3.3; AI-unavailable capping 3.4).
    verdict = build_verdict(
        plan,
        first_page,
        page_title=capture.page_title,
        main_status=capture.main_status,
        min_reviews=settings.viability_min_reviews,
        reported_total_multiplier=settings.viability_reported_total_multiplier,
        max_rejection_fraction=settings.viability_max_rejection_fraction,
        robots_warning=robots_warning,
    )
    return verdict, plan


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string for a blocked plan."""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
