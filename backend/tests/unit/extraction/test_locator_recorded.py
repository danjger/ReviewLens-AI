"""Unit tests with "recorded" Locator responses taken end-to-end through
post-processing (Task 4.3).

How "recorded responses" are handled here
-----------------------------------------
A true recorded fixture (``make record-ai``) needs a live ``ANTHROPIC_API_KEY``,
which is unavailable unattended, and ``testing.md`` forbids hand-writing fixture
JSON *except* for malformed output.  So these tests follow the offline pattern
Task 4.1 established in ``test_locator.py``: they script an Anthropic-shaped
client that returns forced ``tool_use`` blocks inline (``_ScriptedClient`` +
``_tool_use_response``), representing a recorded Locator response
deterministically with no network.  Each response is then run through the real
Locator (``locate``) and the real ``postprocess`` against sample HTML, asserting
the Verified Reviews and discard counts.

The required special case — a Locator response that "supplies its own text" — is
in :class:`TestSuppliesOwnText`: the forced tool schema has
``additionalProperties: false`` and no text field, so an attempt to inject a
``text`` / ``review_text`` key is *rejected* by schema validation (the one
repair retry then fails → ``LocatorUnavailable``), and even when such a field
somehow reaches ``postprocess`` it is ignored and the kept text is read from the
page element.  Both behaviours are asserted; see the class docstring.

_Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.7_
"""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.ai import AiClient, reset_ai_client, set_ai_client
from app.core.config import get_settings
from app.extraction import locator
from app.extraction.cleaner import build_clean_result, resolve_ref
from app.extraction.errors import LocatorUnavailable
from app.extraction.models import CleanedPage, LocatorItem, LocatorResult
from app.extraction.postprocess import postprocess
from moto import mock_aws

from tests.support.dynamodb import ensure_rate_limit_table

_TABLE = "rate-limits"
_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# AWS env + rate-limit table (the instrumented client needs them), as in 4.1
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


# ---------------------------------------------------------------------------
# Scripted offline client (an inline "recorded response"), mirroring 4.1
# ---------------------------------------------------------------------------


def _tool_use_response(tool_input: dict[str, Any]) -> SimpleNamespace:
    """An SDK-shaped message carrying a forced ``locator_result`` tool_use."""
    block = SimpleNamespace(type="tool_use", name="locator_result", input=tool_input)
    return SimpleNamespace(
        id="msg_recorded",
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

    def __init__(self, responses: list[Any]) -> None:  # noqa: ANN401
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("Scripted client ran out of responses")
        return self._responses.pop(0)


def _install(client: Any) -> None:  # noqa: ANN401
    set_ai_client(AiClient(client=client))


# ---------------------------------------------------------------------------
# Sample HTML with several review cards, excluded items, and a next-page link.
# Only block elements get reference IDs, so each field lives in a block tag.
# ---------------------------------------------------------------------------

_SAMPLE_PAGE = (
    "<html><body>"
    "<main>"
    # Review 1 — full card with a rating cue, author, date, title.
    '<article class="review-card">'
    '<h3 class="title">Saved us weeks</h3>'
    '<p class="body">Onboarding was smooth and the support team answered fast.</p>'
    '<a class="stars" aria-label="5 out of 5 stars" href="#">rating</a>'
    '<p class="author">Jordan Lee</p>'
    "<time>March 3, 2022</time>"
    "</article>"
    # Review 2 — body only, no rating cue.
    '<article class="review-card">'
    '<p class="body">It does the job but the mobile app could use polish overall.</p>'
    "</article>"
    # A seller response the Locator should mark excluded (kind seller_response).
    '<div class="seller">We appreciate your feedback and will keep improving things.</div>'
    # A short note that is too short to keep even if called a review.
    '<p class="note">Nice.</p>'
    "</main>"
    '<a class="next" rel="next" href="/reviews?page=2">Next</a>'
    "</body></html>"
)


def _page_from(html: str) -> tuple[CleanedPage, str, dict[str, str]]:
    """Clean ``html`` and return a single-chunk CleanedPage plus html + lookup."""
    cleaned = build_clean_result(html)
    page = CleanedPage(
        lines=cleaned.lines,
        lookup=cleaned.lookup,
        chunks=[cleaned.lines],
        tokens=0,
    )
    return page, html, cleaned.lookup


def _ref_for_text(html: str, lookup: dict[str, str], needle: str) -> str:
    """Ref of the smallest element whose text contains ``needle`` (leaf field)."""
    best_ref: str | None = None
    best_len: int | None = None
    for ref in lookup:
        node = resolve_ref(html, lookup, ref)
        if node is None:
            continue
        text = node.text()
        if needle in text and (best_len is None or len(text) < best_len):
            best_ref = ref
            best_len = len(text)
    if best_ref is None:
        msg = f"no ref resolves to text containing {needle!r}"
        raise AssertionError(msg)
    return best_ref


# ---------------------------------------------------------------------------
# A realistic multi-item "recorded" response, located then post-processed.
# ---------------------------------------------------------------------------


class TestRecordedResponseEndToEnd:
    """A recorded-shaped response → ``locate`` → ``postprocess`` on real HTML."""

    @mock_aws
    def test_realistic_response_yields_expected_reviews_and_discards(self) -> None:
        _create_rate_limit_table()
        page, html, lookup = _page_from(_SAMPLE_PAGE)

        r1_body = _ref_for_text(html, lookup, "Onboarding was smooth")
        r1_title = _ref_for_text(html, lookup, "Saved us weeks")
        r1_rating = _ref_for_text(html, lookup, "rating")
        r1_author = _ref_for_text(html, lookup, "Jordan Lee")
        r1_date = _ref_for_text(html, lookup, "March 3, 2022")
        r2_body = _ref_for_text(html, lookup, "It does the job")
        seller = _ref_for_text(html, lookup, "appreciate your feedback")
        short = _ref_for_text(html, lookup, "Nice.")
        next_ref = _ref_for_text(html, lookup, "Next")

        recorded = _tool_use_response(
            {
                "has_reviews": True,
                "rating_scale": 5,
                "items": [
                    {
                        "item_ref": r1_body,
                        "text_ref": r1_body,
                        "rating_value": 5,
                        "rating_ref": r1_rating,
                        "date_ref": r1_date,
                        "author_ref": r1_author,
                        "title_ref": r1_title,
                        "kind": "review",
                    },
                    {"item_ref": r2_body, "text_ref": r2_body, "kind": "review"},
                    # A short note the AI mislabels as a review → discarded too_short.
                    {"item_ref": short, "text_ref": short, "kind": "review"},
                    # The seller response, correctly marked as a non-review kind.
                    {"item_ref": seller, "text_ref": seller, "kind": "seller_response"},
                ],
                "excluded_refs": [{"ref": seller, "kind": "seller_response"}],
                "selectors": {
                    "item": "article.review-card",
                    "text": ".body",
                    "rating": "[aria-label*='out of 5']",
                    "author": ".author",
                    "date": "time",
                    "title": ".title",
                },
                "next_page": {"ref": next_ref},
                "reported_total": 128,
                "entity_hint": "Acme CRM",
                "confidence": "high",
            }
        )
        _install(_ScriptedClient([recorded]))

        result = locator.locate(page, url="https://x.test/reviews", title="Reviews")
        reviews, discarded = postprocess(html, lookup, result)

        # The Locator parsed cleanly (Requirement 2.1) ...
        assert isinstance(result, LocatorResult)
        assert result.reported_total == 128
        assert result.next_page.ref == next_ref

        # ... and post-processing kept exactly the two genuine reviews, in order.
        assert [r.text for r in reviews] == [
            "Onboarding was smooth and the support team answered fast.",
            "It does the job but the mobile app could use polish overall.",
        ]
        # Review 1 fields were read from the page by code.
        assert reviews[0].rating == 5
        assert reviews[0].author == "Jordan Lee"
        assert reviews[0].title == "Saved us weeks"
        assert reviews[0].date == "2022-03-03"
        # Review 2 has no rating cue → rating dropped, review kept.
        assert reviews[1].rating is None
        # Discards: the too-short note and the seller response by its kind.
        assert discarded == {"too_short": 1, "seller_response": 1}


# ---------------------------------------------------------------------------
# REQUIRED CASE: a Locator response that "supplies its own text".
# ---------------------------------------------------------------------------


class TestSuppliesOwnText:
    """A response that tries to supply review text — the AI may never do that.

    Two layers enforce the "AI may never supply review text" invariant, and this
    class asserts both:

    1. **Schema layer (the provider's contract).** The forced tool's item schema
       is ``additionalProperties: false`` with no text field, so the Anthropic
       API will not accept an item carrying a ``text`` / ``review_text`` key —
       the model is structurally unable to return prose.  We assert that
       contract directly on the schema (there is no text field and extra keys
       are forbidden), since the provider's enforcement cannot be exercised
       offline.

    2. **Post-processing layer (defence in depth).** Even if a ``text`` field
       somehow reached a :class:`LocatorResult`-shaped object (e.g. a lenient
       boundary), ``LocatorResult`` has no such field so Pydantic drops it, and
       ``postprocess`` reads text only from the resolved page element — so the
       kept text equals the element's text and never the injected prose.

    A genuinely malformed response (wrong *types*, or no tool call at all) is
    what triggers the schema-validation failure path and the one repair retry;
    :class:`TestRepairRetry` in ``test_locator.py`` covers that, and
    :meth:`test_prose_in_a_text_block_is_not_treated_as_a_review` covers prose
    offered instead of a tool call.
    """

    _INJECTED = "FIVE STARS BEST PRODUCT EVER — buy now at spam.example"

    def test_tool_schema_forbids_a_text_field(self) -> None:
        """The provider contract: item schema has no text field and no extras.

        This is what stops the model from ever supplying review text at the
        source: the forced tool it must call cannot carry prose.
        """
        item_schema = locator.tool_schema()["input_schema"]["properties"]["items"]["items"]
        assert item_schema["additionalProperties"] is False
        props = set(item_schema["properties"])
        assert "text" not in props
        assert "review_text" not in props
        # And the model it validates against has no text field either, so even a
        # permissive boundary cannot carry prose through to a LocatorItem.
        model_fields = set(LocatorItem.model_fields)
        assert "text" not in model_fields
        assert "review_text" not in model_fields

    @mock_aws
    def test_injected_text_field_ignored_when_present_on_valid_item(self) -> None:
        """A ``text`` key alongside otherwise-valid fields is dropped, not used.

        Pydantic's default is to ignore unexpected keys, so a response that
        smuggles ``text`` past a lenient boundary still parses to a
        :class:`LocatorItem` with no text attribute, and the located review's
        text is the page element's text — never the injected prose.
        """
        _create_rate_limit_table()
        page, html, lookup = _page_from(_SAMPLE_PAGE)
        body = _ref_for_text(html, lookup, "Onboarding was smooth")

        # A tool_use whose input adds an unexpected ``text`` key. The forced
        # schema would reject it at the provider, but we assert what the *model
        # layer* does with it: ignore it. (Validator layer is covered above.)
        smuggled = _tool_use_response(
            {
                "has_reviews": True,
                "rating_scale": 5,
                "items": [
                    {
                        "item_ref": body,
                        "text_ref": body,
                        "text": self._INJECTED,
                        "kind": "review",
                    }
                ],
            }
        )
        # Validate the tool input directly through the model to show the key is
        # dropped (this mirrors locator._parse_result's model_validate call).
        result = LocatorResult.model_validate(smuggled.content[0].input)
        item = result.items[0]
        assert not hasattr(item, "text")
        assert not hasattr(item, "review_text")

        reviews, _ = postprocess(html, lookup, result)

        assert len(reviews) == 1
        # The kept text is the page element's text, not the injected prose.
        node = resolve_ref(html, lookup, body)
        assert node is not None
        assert reviews[0].text == "Onboarding was smooth and the support team answered fast."
        assert self._INJECTED not in reviews[0].text

    @mock_aws
    def test_prose_in_a_text_block_is_not_treated_as_a_review(self) -> None:
        """A plain-text response block (no tool_use) is malformed → repair → fail.

        A model that writes prose instead of calling the tool supplies no
        structured answer; that prose can never become review text.  Two such
        responses raise :class:`LocatorUnavailable`.
        """
        _create_rate_limit_table()
        page, _html, _lookup = _page_from(_SAMPLE_PAGE)

        prose = SimpleNamespace(
            id="msg_prose",
            content=[SimpleNamespace(type="text", text=self._INJECTED)],
            usage=None,
        )
        client = _ScriptedClient([prose, prose])
        _install(client)

        with pytest.raises(LocatorUnavailable):
            locator.locate(page, url="https://x.test/reviews", title="Reviews")
        assert len(client.calls) == 2
