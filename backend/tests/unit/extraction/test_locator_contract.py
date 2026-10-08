"""Schema contract test: the forced Locator tool schema and the Pydantic model
stay in sync (Task 4.3; design "Contract test: the Locator tool schema and the
Pydantic model stay in sync").

The Review Locator forces the model through a tool whose ``input_schema`` is
hand-written in :func:`app.extraction.locator.tool_schema`, while
:class:`app.extraction.models.LocatorResult` (and its sub-models) is what the
response is validated against.  If the two drift — a field added to one but not
the other, or an enum that gains a value on one side only — the Locator would
either reject valid model answers or accept inputs the model cannot represent.

These tests pin the two together:

- Top-level ``LocatorResult`` fields == schema ``properties`` keys.
- ``LocatorItem`` fields == item sub-schema ``properties`` keys, and the schema
  marks ``item_ref`` required (the only field without a default on the model).
- ``ExcludedItem`` / ``LocatorSelectors`` / ``LocatorNextPage`` fields ==
  their sub-schema ``properties`` keys.
- The schema enums for ``kind`` (``ItemKind``), ``blocker`` (``Blocker``), and
  ``confidence`` (``Confidence``) match the model's ``Literal`` members.
- The item schema still has **no** field that could carry review text
  (steering: the AI may point at elements, never supply review text).

_Validates: Requirements 2.1, 2.2_
"""

from __future__ import annotations

import typing

from app.extraction import locator
from app.extraction.models import (
    Blocker,
    Confidence,
    ExcludedItem,
    ItemKind,
    LocatorItem,
    LocatorNextPage,
    LocatorResult,
    LocatorSelectors,
)


def _schema() -> dict[str, typing.Any]:
    return locator.tool_schema()


def _top_properties() -> dict[str, typing.Any]:
    return _schema()["input_schema"]["properties"]


def _item_properties() -> dict[str, typing.Any]:
    return _top_properties()["items"]["items"]["properties"]


def _literal_members(tp: typing.Any) -> set[str]:  # noqa: ANN401 - Literal alias
    """Return the member strings of a ``Literal[...]`` type alias."""
    return set(typing.get_args(tp))


# ---------------------------------------------------------------------------
# Field names: schema properties == model fields
# ---------------------------------------------------------------------------


class TestFieldNamesInSync:
    def test_top_level_fields_match(self) -> None:
        """Every LocatorResult field is a schema property and vice versa."""
        assert set(_top_properties()) == set(LocatorResult.model_fields)

    def test_item_fields_match(self) -> None:
        """Every LocatorItem field is an item sub-schema property and vice versa."""
        assert set(_item_properties()) == set(LocatorItem.model_fields)

    def test_excluded_fields_match(self) -> None:
        excluded_props = _top_properties()["excluded_refs"]["items"]["properties"]
        assert set(excluded_props) == set(ExcludedItem.model_fields)

    def test_selectors_fields_match(self) -> None:
        selectors_props = _top_properties()["selectors"]["properties"]
        assert set(selectors_props) == set(LocatorSelectors.model_fields)

    def test_next_page_fields_match(self) -> None:
        next_page_props = _top_properties()["next_page"]["properties"]
        assert set(next_page_props) == set(LocatorNextPage.model_fields)


# ---------------------------------------------------------------------------
# Required markers reflect which model fields lack a default
# ---------------------------------------------------------------------------


class TestRequiredInSync:
    def test_item_requires_only_item_ref(self) -> None:
        """``item_ref`` is the only LocatorItem field without a default.

        A model field with no default must be required in the schema, and a
        field with a default must not be, or the model and schema disagree about
        what a minimal valid item looks like.
        """
        required = set(_top_properties()["items"]["items"].get("required", []))
        model_required = {
            name for name, field in LocatorItem.model_fields.items() if field.is_required()
        }
        assert required == model_required == {"item_ref"}

    def test_excluded_requires_ref_and_kind(self) -> None:
        required = set(_top_properties()["excluded_refs"]["items"].get("required", []))
        model_required = {
            name for name, field in ExcludedItem.model_fields.items() if field.is_required()
        }
        assert required == model_required == {"ref", "kind"}

    def test_result_requires_has_reviews(self) -> None:
        required = set(_schema()["input_schema"].get("required", []))
        model_required = {
            name for name, field in LocatorResult.model_fields.items() if field.is_required()
        }
        # has_reviews has a default on the model but is the one field the schema
        # pins as required so the model always states review presence explicitly.
        assert "has_reviews" in required
        assert model_required == set()


# ---------------------------------------------------------------------------
# Enums: schema enum members == model Literal members
# ---------------------------------------------------------------------------


class TestEnumsInSync:
    def test_item_kind_enum_matches_literal(self) -> None:
        kinds = set(_item_properties()["kind"]["enum"])
        assert kinds == _literal_members(ItemKind)

    def test_excluded_kind_enum_matches_literal(self) -> None:
        excluded_props = _top_properties()["excluded_refs"]["items"]["properties"]
        assert set(excluded_props["kind"]["enum"]) == _literal_members(ItemKind)

    def test_blocker_enum_matches_literal(self) -> None:
        # The schema lists the blockers plus an explicit null (nullable field).
        enum = set(_top_properties()["blocker"]["enum"])
        assert enum == _literal_members(Blocker) | {None}

    def test_confidence_enum_matches_literal(self) -> None:
        enum = set(_top_properties()["confidence"]["enum"])
        assert enum == _literal_members(Confidence) | {None}


# ---------------------------------------------------------------------------
# No-text invariant on the schema itself
# ---------------------------------------------------------------------------


class TestNoTextField:
    def test_item_schema_rejects_extra_properties(self) -> None:
        """additionalProperties:false means an injected ``text`` key is invalid."""
        item_schema = _top_properties()["items"]["items"]
        assert item_schema["additionalProperties"] is False

    def test_item_schema_has_no_text_bearing_field(self) -> None:
        """No item field accepts review text — only refs, rating value, and kind."""
        props = set(_item_properties())
        assert "text" not in props
        assert "review_text" not in props
        # Every string-valued pointer ends in ``_ref``; the non-ref fields are
        # the numeric rating and the kind enum, neither of which carries prose.
        non_ref = {name for name in props if not name.endswith("_ref")}
        assert non_ref == {"rating_value", "kind"}
