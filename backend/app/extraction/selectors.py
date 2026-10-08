"""Selector validation for the Extraction Engine.

When the Review Locator suggests CSS selectors (so later pages can be read
cheaply by code instead of by the AI), those suggestions are only trustworthy
if they actually reproduce the reviews we already verified on this page.  This
module applies the suggested ``item`` selector to the *original* HTML and
measures two quantities against the Verified Reviews (Requirement 4.1):

- **Agreement** — ``|matched verified texts| / |verified|``: the share of
  Verified Reviews whose text is reproduced by some selected item.
- **Over-selection** — ``|extra items| / |selected|``: the share of selected
  items that match no Verified Review (noise the selector would scoop up).

Selectors are valid only when ``agreement >= min_agreement`` (default 0.8, from
``selector_min_agreement``) **and** ``over_selection <= SELECTOR_MAX_OVER_SELECTION``
(0.2, fixed per the design).  The plan builder (Task 5.2) uses the result to
choose the ``selectors`` method per Requirement 4.2.

Design: see ``.kiro/specs/review-extraction/design.md`` ("Selector validation
and plan").  This satisfies Property 8 ("Selector validation is sound"): a
selector marked valid reproduces at least the configured share of verified
texts.

Text matching uses **normalized containment**, not equality.  Verified text was
read by code from a *field* element (e.g. ``.review-body``) while the ``item``
selector points at the *wrapper* card, whose text contains the body plus the
author, date, rating label, and so on.  So a selected item "matches" a verified
review when the verified review's normalized text is a substring of the item's
normalized text.  Normalization is the cleaner's shared whitespace collapse, so
"identical text" means identical after the same normalization applied
everywhere else in the package.

Purity: :func:`validate_selectors` takes ``min_agreement`` as a parameter and
reads no configuration, so it is deterministic and trivially testable.  The
convenience :func:`validate_selectors_from_config` reads
``get_settings().selector_min_agreement`` for callers that want the configured
default.
"""

from __future__ import annotations

from dataclasses import dataclass

from selectolax.parser import HTMLParser, Node

from app.extraction.cleaner import _WS_RE
from app.extraction.models import LocatorSelectors, VerifiedReview

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum share of selected items that may match no Verified Review for the
#: selectors to still be considered valid (Requirement 4.1: "select no more
#: than 20% extra elements").  Fixed by the design; unlike the agreement
#: threshold it is not configurable.
SELECTOR_MAX_OVER_SELECTION = 0.2


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectorValidation:
    """Outcome of validating a Locator's suggested selectors.

    ``is_valid`` is the final verdict; ``agreement`` and ``over_selection`` are
    the measured ratios the verdict was derived from, carried through so the
    plan builder (Task 5.2) can record or compare them.  ``selected_count`` and
    ``matched_verified`` are the raw counts behind the ratios, kept for logging
    and tests.
    """

    is_valid: bool
    agreement: float
    over_selection: float
    selected_count: int
    matched_verified: int


# A validation that could not even be attempted (no item selector, no verified
# reviews, or a selector that threw): zero agreement, invalid.  Over-selection
# is reported as 0.0 because there were no selected items to be "extra".
_INVALID = SelectorValidation(
    is_valid=False,
    agreement=0.0,
    over_selection=0.0,
    selected_count=0,
    matched_verified=0,
)


# ---------------------------------------------------------------------------
# Text normalization and reading
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Collapse whitespace the same way the rest of the package does.

    Uses the cleaner's shared regex so "identical text" in validation means
    identical under the normalization applied when reading review text in
    post-processing.
    """
    return _WS_RE.sub(" ", text).strip()


def _node_text(node: Node) -> str:
    """Return a selected element's normalized visible text."""
    return _normalize(node.text())


def _select_items(html: str, item_selector: str) -> list[Node] | None:
    """Apply ``item_selector`` to ``html`` and return the matched elements.

    Returns ``None`` when the selector throws on the page — selectolax raises on
    malformed or unsupported selectors.  Per the design's Error Handling table
    ("Selectors throw on a page → treated as zero yield"), the caller treats a
    ``None`` here as a failed validation rather than crashing.
    """
    try:
        tree = HTMLParser(html)
        return list(tree.css(item_selector))
    except Exception:  # noqa: BLE001 - any selector error means "zero yield"
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_selectors(
    html: str,
    selectors: LocatorSelectors,
    verified: list[VerifiedReview],
    *,
    min_agreement: float,
) -> SelectorValidation:
    """Validate suggested selectors against the Verified Reviews (Requirement 4.1).

    Applies ``selectors.item`` to the original ``html`` and measures how well
    the selected items reproduce ``verified``:

    - **agreement** = (Verified Reviews whose normalized text is contained in
      some selected item's normalized text) / (number of Verified Reviews);
    - **over_selection** = (selected items matching no Verified Review) /
      (number of selected items).

    The selectors are valid only when ``agreement >= min_agreement`` **and**
    ``over_selection <= SELECTOR_MAX_OVER_SELECTION``.  Matching uses normalized
    containment because the ``item`` selector points at the wrapper card whose
    text contains the verified field text (see the module docstring).

    Edge cases, all returning an invalid result (so a bad suggestion can never
    be chosen):

    - no ``item`` selector (``None`` or blank) → cannot select anything;
    - empty ``verified`` list → nothing to agree with, so agreement is
      undefined; treated as invalid (we never trust selectors we can't check);
    - a selector that throws on the page → treated as zero yield;
    - a selector that selects nothing → zero agreement.

    :param html: The original rendered HTML the reviews were verified against.
    :param selectors: The Locator's suggested selectors; only ``item`` is used
        for the agreement/over-selection measure.
    :param verified: The Verified Reviews read from this page by code.
    :param min_agreement: Minimum agreement ratio required (``0 < x <= 1``);
        callers pass ``get_settings().selector_min_agreement`` for the default.
    :returns: The :class:`SelectorValidation` with the verdict and the measured
        ratios and counts.
    """
    item_selector = selectors.item
    if item_selector is None or not item_selector.strip():
        return _INVALID
    if not verified:
        return _INVALID

    selected = _select_items(html, item_selector)
    if selected is None:
        return _INVALID

    selected_count = len(selected)
    if selected_count == 0:
        return _INVALID

    selected_texts = [_node_text(node) for node in selected]
    verified_texts = [_normalize(review.text) for review in verified]

    # A verified review is "matched" when its (non-empty) normalized text is a
    # substring of some selected item's normalized text.
    matched_verified = sum(
        1
        for v_text in verified_texts
        if v_text and any(v_text in s_text for s_text in selected_texts)
    )

    # A selected item is "extra" when it reproduces none of the verified texts.
    extra_items = sum(
        1
        for s_text in selected_texts
        if not any(v_text and v_text in s_text for v_text in verified_texts)
    )

    agreement = matched_verified / len(verified_texts)
    over_selection = extra_items / selected_count

    is_valid = agreement >= min_agreement and over_selection <= SELECTOR_MAX_OVER_SELECTION

    return SelectorValidation(
        is_valid=is_valid,
        agreement=agreement,
        over_selection=over_selection,
        selected_count=selected_count,
        matched_verified=matched_verified,
    )


def validate_selectors_from_config(
    html: str,
    selectors: LocatorSelectors,
    verified: list[VerifiedReview],
) -> SelectorValidation:
    """Validate selectors using the configured ``selector_min_agreement``.

    Convenience wrapper over :func:`validate_selectors` for callers that want
    the configured default (0.8) without threading settings through themselves.
    Prefer :func:`validate_selectors` with an explicit ``min_agreement`` in
    tests so the pure function is exercised deterministically.
    """
    from app.core.config import get_settings  # noqa: PLC0415 - avoid import cycle at load

    return validate_selectors(
        html,
        selectors,
        verified,
        min_agreement=get_settings().selector_min_agreement,
    )
