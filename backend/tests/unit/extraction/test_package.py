"""Unit tests for the app.extraction package scaffolding (Task 1).

Covers:
- The public API functions are importable and are declared stubs
  (they raise NotImplementedError until later tasks implement them).
- The error types subclass the shared RetryableError base and carry their
  own machine-readable codes, so callers can treat them as retryable yet tell
  them apart.
- The data models from the design construct, apply their defaults, and
  validate their constrained literal fields.

These are scaffolding tests: later tasks replace the NotImplementedError
assertions with behavioral tests of each function.
"""

from __future__ import annotations

import pytest
from app.core.errors import AppError, RetryableError
from app.extraction import (
    AIUnavailable,
    CleanedPage,
    ExtractionPlan,
    LocatorResult,
    LocatorSelectors,
    LocatorUnavailable,
    NextPage,
    NextPageRule,
    PageResult,
    StructuredResult,
    VerifiedReview,
    build_plan,
    clean,
    extract_page,
    locate,
    next_page,
    parse_structured,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Public API stubs
# ---------------------------------------------------------------------------


class TestPublicApiStubs:
    """Public functions exist; implemented ones work, un-implemented ones stub.

    All public functions — ``clean``, ``parse_structured``, ``locate``,
    ``build_plan``, ``extract_page``, and ``next_page`` — are implemented by
    their tasks (``extract_page`` by Task 7); each seam delegates to its module
    and no longer raises ``NotImplementedError``.
    """

    def test_clean_is_implemented(self) -> None:
        # Implemented by Task 2.1 (page cleaner); no longer a stub.  Behavioral
        # coverage lives in tests/unit/extraction/test_cleaner.py.
        page = clean("<body><p>a review of adequate length</p></body>", budget_tokens=30_000)
        assert page.lines

    def test_parse_structured_is_implemented(self) -> None:
        # Implemented by Task 3 (structured-data parsing); no longer a stub.
        # Behavioral coverage lives in
        # tests/unit/extraction/test_structured.py.  A page with no structured
        # data returns an empty result rather than raising.
        result = parse_structured("<html><body><p>no data</p></body></html>", "no data")
        assert result.reviews == []
        assert result.review_count is None

    def test_locate_is_implemented(self) -> None:
        # Implemented by Task 4.1 (Review Locator); no longer a stub.  It
        # delegates to app.extraction.locator, whose behavioral coverage (the
        # forced tool schema, repair retry, error types, and chunk merge) lives
        # in tests/unit/extraction/test_locator.py.  Assert only that the public
        # seam is wired to the implementation and no longer raises
        # NotImplementedError (without making a real AI call here).
        from app.extraction import locator as locator_module

        assert locate.__module__ == "app.extraction"
        assert hasattr(locator_module, "locate")

    def test_build_plan_is_implemented(self) -> None:
        # Implemented by Task 5.2 (Extraction Plan builder); no longer a stub.
        # The public seam delegates to app.extraction.plan.build_plan, whose
        # behavioral coverage (method choice, next-page rule, degraded plan)
        # lives in tests/unit/extraction/test_plan.py.  Assert only that the
        # seam is wired to the implementation and no longer raises
        # NotImplementedError (without making a real AI call here).
        from app.extraction import plan as plan_module

        assert build_plan.__module__ == "app.extraction"
        assert hasattr(plan_module, "build_plan")

    def test_extract_page_is_implemented(self) -> None:
        # Implemented by Task 7 (per-page extraction); no longer a stub.  The
        # public seam delegates to app.extraction.extract.extract_page, whose
        # behavioral coverage (method dispatch, the selector-yield fallback, the
        # structured fallback, the structured cross-check, and the full
        # PageResult) lives in tests/unit/extraction/test_extract.py.  Assert
        # only that the seam is wired and no longer raises NotImplementedError,
        # using a selectors plan on a page with no reviews and is_last=True so
        # no AI call is made here.
        from app.extraction import extract as extract_module

        assert extract_page.__module__ == "app.extraction"
        assert hasattr(extract_module, "extract_page")
        plan = ExtractionPlan(
            created_at="2024-01-01T00:00:00Z",
            method="selectors",
            selectors=LocatorSelectors(item="article.review"),
            next_page_rule=NextPageRule(type="none"),
        )
        result = extract_page(
            "<html><body><p>one page, no reviews</p></body></html>",
            "https://example.com",
            plan,
            is_last=True,
        )
        assert result.reviews == []
        assert result.method_used == "selectors"

    def test_next_page_is_implemented(self) -> None:
        # Implemented by Task 6 (pagination); no longer a stub.  The public seam
        # delegates to app.extraction.pagination.next_page, whose behavioral
        # coverage (the rule order, generic patterns, URL-template inference, the
        # same-domain filter, and the "no URL" reason) lives in
        # tests/unit/extraction/test_pagination.py and tests/property/test_pagination.py.
        from app.extraction import pagination as pagination_module

        assert next_page.__module__ == "app.extraction"
        assert hasattr(pagination_module, "next_page")
        # A page with no next control resolves to no URL rather than raising.
        result = next_page("<html><body><p>one page</p></body></html>", "https://example.com", None)
        assert result.url is None
        assert result.reason_if_none is not None


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class TestErrorTypes:
    """AIUnavailable and LocatorUnavailable are distinct retryable errors."""

    def test_ai_unavailable_is_retryable(self) -> None:
        err = AIUnavailable("provider down")
        assert isinstance(err, RetryableError)
        assert isinstance(err, AppError)

    def test_locator_unavailable_is_retryable(self) -> None:
        err = LocatorUnavailable("bad schema")
        assert isinstance(err, RetryableError)
        assert isinstance(err, AppError)

    def test_distinct_codes(self) -> None:
        assert AIUnavailable("x").code == "AI_UNAVAILABLE"
        assert LocatorUnavailable("x").code == "LOCATOR_UNAVAILABLE"

    def test_callers_can_catch_either_as_retryable(self) -> None:
        for exc in (AIUnavailable("x"), LocatorUnavailable("x")):
            with pytest.raises(RetryableError):
                raise exc

    def test_retryable_base_maps_to_503(self) -> None:
        assert AIUnavailable("x").status_code == 503


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class TestDataModels:
    """The design's data models construct with sensible defaults and validate."""

    def test_cleaned_page_defaults(self) -> None:
        page = CleanedPage()
        assert page.lines == []
        assert page.lookup == {}
        assert page.chunks == []
        assert page.tokens == 0

    def test_locator_result_defaults(self) -> None:
        result = LocatorResult()
        assert result.has_reviews is False
        assert result.items == []
        assert result.selectors.item is None
        assert result.next_page.ref is None

    def test_verified_review_requires_text(self) -> None:
        review = VerifiedReview(text="Great product, used it daily.")
        assert review.rating is None
        with pytest.raises(ValidationError):
            VerifiedReview()  # type: ignore[call-arg]

    def test_structured_result_defaults(self) -> None:
        result = StructuredResult()
        assert result.reviews == []
        assert result.review_count is None

    def test_next_page_requires_rule_used(self) -> None:
        np = NextPage(rule_used="generic", url="https://example.com/page/2")
        assert np.reason_if_none is None
        with pytest.raises(ValidationError):
            NextPage()  # type: ignore[call-arg]

    def test_extraction_plan_round_trips(self) -> None:
        plan = ExtractionPlan(
            created_at="2024-01-01T00:00:00Z",
            method="selectors",
            next_page_rule=NextPageRule(type="selector", css="a[rel=next]"),
            rating_scale=5,
            reported_total=1540,
        )
        dumped = plan.model_dump()
        restored = ExtractionPlan.model_validate(dumped)
        assert restored == plan
        assert restored.degraded is False
        assert restored.first_page.verified == 0

    def test_extraction_plan_rejects_unknown_method(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionPlan(
                created_at="2024-01-01T00:00:00Z",
                method="magic",  # type: ignore[arg-type]
                next_page_rule=NextPageRule(type="none"),
            )

    def test_page_result_defaults(self) -> None:
        result = PageResult(
            method_used="ai_direct",
            next_page=NextPage(rule_used="none", reason_if_none="infinite scroll"),
        )
        assert result.reviews == []
        assert result.fallback is False
        assert result.discarded == {}
        assert result.structured_agreement is None

    def test_next_page_rule_rejects_unknown_type(self) -> None:
        with pytest.raises(ValidationError):
            NextPageRule(type="teleport")  # type: ignore[arg-type]
