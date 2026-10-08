"""Unit tests for the Extraction Plan builder (``app.extraction.plan``), Task 5.2.

Covers :func:`app.extraction.build_plan`, which composes the cleaner, structured
parser, Locator, post-processing, and selector validation into an
:class:`ExtractionPlan` plus the first-page :class:`PageResult` (Requirement
4.2, 4.3, 4.4).

These are focused wiring tests of ``build_plan`` itself; the full method-choice
table and Property 8 are Task 5.3.  They exercise:

- selectors valid → ``method="selectors"``, plan fields populated, degraded False;
- selectors invalid + structured has *more* and texts agree → ``structured``;
- selectors invalid + structured not better → ``ai_direct``;
- ``AIUnavailable`` from the Locator → degraded structured-only plan;
- ``LocatorUnavailable`` propagates (not degraded);
- the plan records ``locator_model`` from config and ``prompt_version``;
- ``created_at`` is injectable (the only non-deterministic field).

The AI is stubbed with the inline ``tool_use`` pattern from ``test_locator.py``;
``locate`` hits the rate limiter, so the DynamoDB rate-limit table is created
with moto ``mock_aws``.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.ai import AiClient, reset_ai_client, set_ai_client
from app.core.config import get_settings
from app.extraction.errors import LocatorUnavailable
from app.extraction.models import ExtractionPlan, PageResult
from app.extraction.plan import build_plan
from moto import mock_aws

from tests.support.dynamodb import ensure_rate_limit_table

_TABLE = "rate-limits"
_REGION = "us-east-1"

#: A fixed clock so ``created_at`` is deterministic in assertions.
_FIXED_NOW = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)


# ---------------------------------------------------------------------------
# AWS / AI stub plumbing (mirrors test_locator.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_RATE_LIMIT_TABLE", _TABLE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    reset_ai_client()


def _create_rate_limit_table() -> None:
    """Create the ``rate-limits`` table (idempotent, shared schema)."""
    ensure_rate_limit_table(_TABLE, region=_REGION)


def _tool_use_response(tool_input: dict[str, Any]) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name="locator_result", input=tool_input)
    return SimpleNamespace(
        id="msg_test",
        content=[block],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class _ScriptedClient:
    """Anthropic-like client returning queued responses in order."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create, count_tokens=_count_tokens)

    def _create(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("Scripted client ran out of responses")
        return self._responses.pop(0)


class _RaisingClient:
    """Anthropic-like client whose create() raises, simulating a provider error.

    ``count_tokens`` still succeeds so the cleaner (which runs before the
    Locator) can size the page; only the Locator's ``create`` call fails.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create, count_tokens=_count_tokens)

    def _create(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append(kwargs)
        raise self._exc


def _count_tokens(**kwargs: Any) -> SimpleNamespace:  # noqa: ANN401
    """Stub token counter: a small count keeps every test page under budget."""
    return SimpleNamespace(input_tokens=100)


def _install(client: Any) -> None:  # noqa: ANN401
    set_ai_client(AiClient(client=client))


def _now() -> datetime:
    return _FIXED_NOW


# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

#: Five review cards, each a body paragraph plus author, inside a list that also
#: carries a "Next" pagination link.  Element refs are assigned in document
#: order by the cleaner, so we drive the Locator by pointing at refs below.
_BODIES = [
    "Great tool, the setup took an afternoon but it worked well overall here.",
    "Support was responsive and fixed my issue within a single business day.",
    "The dashboard is intuitive and the reports export cleanly to CSV files.",
    "Pricing is fair for a small team and the onboarding was quite painless.",
    "Occasional slowness at peak hours but reliable the rest of the time too.",
]


def _reviews_html(*, with_structured: bool = False, extra_structured: int = 0) -> str:
    """Build a review-list page.

    :param with_structured: embed JSON-LD reviews matching the first bodies.
    :param extra_structured: how many structured reviews to embed (defaults to
        all five when ``with_structured`` and ``extra_structured`` is 0).
    """
    cards = "".join(
        f'<article class="review-card">'
        f'<p class="body">{body}</p>'
        f'<span class="author">Reviewer {i}</span>'
        f"</article>"
        for i, body in enumerate(_BODIES)
    )
    structured_block = ""
    if with_structured:
        count = extra_structured or len(_BODIES)
        payload = {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Acme CRM",
            "aggregateRating": {"@type": "AggregateRating", "reviewCount": count},
            "review": [
                {
                    "@type": "Review",
                    "reviewBody": _BODIES[i],
                    "author": {"@type": "Person", "name": f"R{i}"},
                }
                for i in range(count)
            ],
        }
        structured_block = '<script type="application/ld+json">' + json.dumps(payload) + "</script>"
    return (
        "<html><body>"
        f"{structured_block}"
        '<main><div class="reviews">'
        f"{cards}"
        '</div><a class="next" rel="next" href="/reviews?page=2">Next</a></main>'
        "</body></html>"
    )


def _ref_for_body(html: str, body_index: int) -> str:
    """Return the Locator ref of the body ``<p>`` for a given card.

    Uses the cleaner directly so the test points at the same refs the plan
    builder will resolve.
    """
    from app.extraction import cleaner

    result = cleaner.build_clean_result(html)
    # Point at the body <p> itself (its text is exactly the body), not an
    # ancestor whose text also contains the body plus siblings.
    target = _BODIES[body_index][:40]
    for line in result.lines:
        if "<p>" in line and target in line:
            return line.strip().split(" ", 1)[0]
    raise AssertionError(f"No ref found for body {body_index}")


def _ref_for_next(html: str) -> str:
    from app.extraction import cleaner

    result = cleaner.build_clean_result(html)
    for line in result.lines:
        # The cleaner keeps href but filters class/rel, so match the next link
        # by its href target.
        if "page=2" in line:
            return line.strip().split(" ", 1)[0]
    raise AssertionError("No next-page ref found")


def _items_for_all_cards(html: str) -> list[dict[str, Any]]:
    """Locator items pointing text_ref at each card body (code reads the text)."""
    return [
        {"item_ref": _ref_for_body(html, i), "text_ref": _ref_for_body(html, i), "kind": "review"}
        for i in range(len(_BODIES))
    ]


def _locator_input(html: str, **overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    base: dict[str, Any] = {
        "has_reviews": True,
        "rating_scale": 5,
        "items": _items_for_all_cards(html),
        "selectors": {"item": "article.review-card", "text": "p.body"},
        "next_page": {"ref": _ref_for_next(html)},
        "reported_total": 42,
        "entity_hint": "Acme CRM",
        "confidence": "high",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Method choice: selectors valid
# ---------------------------------------------------------------------------


class TestSelectorsMethod:
    @mock_aws
    def test_valid_selectors_choose_selectors_method(self) -> None:
        _create_rate_limit_table()
        html = _reviews_html()
        _install(_ScriptedClient([_tool_use_response(_locator_input(html))]))

        plan, page = build_plan(html, "https://x.test/reviews", "Reviews", now=_now)

        assert isinstance(plan, ExtractionPlan)
        assert isinstance(page, PageResult)
        assert plan.method == "selectors"
        assert plan.degraded is False
        # Valid selectors are kept on the plan.
        assert plan.selectors.item == "article.review-card"
        # Plan fields populated.
        assert plan.first_page.verified == 5
        assert plan.first_page.per_page_rate == 5
        assert plan.rating_scale == 5
        assert plan.entity_hint == "Acme CRM"
        assert plan.confidence == "high"
        assert plan.reported_total == 42
        assert plan.created_at == _FIXED_NOW.isoformat()
        # First-page result mirrors the chosen method and verified reviews.
        assert page.method_used == "selectors"
        assert len(page.reviews) == 5
        assert page.fallback is False

    @mock_aws
    def test_next_page_rule_is_a_selector(self) -> None:
        _create_rate_limit_table()
        html = _reviews_html()
        _install(_ScriptedClient([_tool_use_response(_locator_input(html))]))

        plan, page = build_plan(html, "https://x.test/reviews", "Reviews", now=_now)

        # The Locator pointed at a next-page element → selector rule derived.
        assert plan.next_page_rule.type == "selector"
        assert plan.next_page_rule.css is not None
        assert page.next_page.rule_used == "plan_rule"


# ---------------------------------------------------------------------------
# Method choice: structured wins
# ---------------------------------------------------------------------------


class TestStructuredMethod:
    @mock_aws
    def test_structured_more_and_agreeing_chooses_structured(self) -> None:
        _create_rate_limit_table()
        html = _reviews_html(with_structured=True)
        # Locator finds only the first two cards and suggests a bad selector, so
        # selector validation fails; structured holds all five (more) and the
        # texts agree → method should be ``structured``.
        _install(
            _ScriptedClient(
                [
                    _tool_use_response(
                        _locator_input(
                            html,
                            items=[
                                {
                                    "item_ref": _ref_for_body(html, 0),
                                    "text_ref": _ref_for_body(html, 0),
                                    "kind": "review",
                                },
                                {
                                    "item_ref": _ref_for_body(html, 1),
                                    "text_ref": _ref_for_body(html, 1),
                                    "kind": "review",
                                },
                            ],
                            selectors={"item": "div.does-not-exist"},
                        )
                    )
                ]
            )
        )

        plan, page = build_plan(html, "https://x.test/reviews", "Reviews", now=_now)

        assert plan.method == "structured"
        assert plan.degraded is False
        # Selectors are not kept when the method is not ``selectors``.
        assert plan.selectors.item is None
        assert plan.first_page.structured_count == 5
        assert plan.first_page.verified == 2
        assert page.method_used == "structured"


# ---------------------------------------------------------------------------
# Method choice: ai_direct fallback
# ---------------------------------------------------------------------------


class TestAiDirectMethod:
    @mock_aws
    def test_invalid_selectors_and_no_better_structured_chooses_ai_direct(self) -> None:
        _create_rate_limit_table()
        html = _reviews_html()  # no structured data
        _install(
            _ScriptedClient(
                [_tool_use_response(_locator_input(html, selectors={"item": "div.nope"}))]
            )
        )

        plan, page = build_plan(html, "https://x.test/reviews", "Reviews", now=_now)

        assert plan.method == "ai_direct"
        assert plan.degraded is False
        assert plan.first_page.structured_count == 0
        assert page.method_used == "ai_direct"


# ---------------------------------------------------------------------------
# Degraded plan (Requirement 4.4) and error propagation
# ---------------------------------------------------------------------------


class TestDegradedAndErrors:
    @mock_aws
    def test_ai_unavailable_builds_degraded_structured_plan(self) -> None:
        _create_rate_limit_table()
        html = _reviews_html(with_structured=True)
        _install(_RaisingClient(RuntimeError("connection reset")))

        plan, page = build_plan(html, "https://x.test/reviews", "Reviews", now=_now)

        assert plan.degraded is True
        assert plan.method == "structured"
        assert plan.selectors.item is None
        assert plan.rating_scale is None
        assert plan.next_page_rule.type == "none"
        # Structured reviews drive both the plan counts and the page result.
        assert plan.first_page.structured_count == 5
        assert plan.reported_total == 5
        assert page.method_used == "structured"
        assert len(page.reviews) == 5
        assert page.next_page.reason_if_none == "ai_unavailable"
        # Provenance still recorded.
        assert plan.prompt_version == "locator_v1"

    @mock_aws
    def test_locator_unavailable_propagates(self) -> None:
        _create_rate_limit_table()
        html = _reviews_html()
        # Two invalid tool responses → LocatorUnavailable (not degraded).
        bad = _tool_use_response({"has_reviews": True, "rating_scale": "five"})
        _install(_ScriptedClient([bad, _tool_use_response({"items": "not-a-list"})]))

        with pytest.raises(LocatorUnavailable):
            build_plan(html, "https://x.test/reviews", "Reviews", now=_now)


# ---------------------------------------------------------------------------
# Provenance and created_at
# ---------------------------------------------------------------------------


class TestProvenance:
    @mock_aws
    def test_plan_records_model_from_config_and_prompt_version(self) -> None:
        _create_rate_limit_table()
        html = _reviews_html()
        _install(_ScriptedClient([_tool_use_response(_locator_input(html))]))

        plan, _ = build_plan(html, "https://x.test/reviews", "Reviews", now=_now)

        # Model ID comes from config (claude_extract_model), never a literal.
        assert plan.locator_model == get_settings().claude_extract_model
        assert plan.prompt_version == "locator_v1"

    @mock_aws
    def test_created_at_defaults_to_now_when_not_injected(self) -> None:
        _create_rate_limit_table()
        html = _reviews_html()
        _install(_ScriptedClient([_tool_use_response(_locator_input(html))]))

        before = datetime.now(UTC)
        plan, _ = build_plan(html, "https://x.test/reviews", "Reviews")
        after = datetime.now(UTC)

        created = datetime.fromisoformat(plan.created_at)
        assert before <= created <= after
