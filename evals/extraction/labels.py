"""Loader and schema for the extraction evaluation labels.

The labelled evaluation set lives next to this module:

- ``pages/<name>/page.html`` — the saved, already-rendered HTML of a page.
- ``labels.yaml`` — one entry per page name with its URL, hand-checked viability
  verdict, layout tags, expected next page, and expected reviews.

This module parses ``labels.yaml`` into typed dataclasses, loads each page's HTML
from disk, and exposes :data:`REQUIRED_LAYOUT_TAGS` — the layout variety the seed
must cover (``review-extraction`` Requirement 8.1). It is pure and offline: it
reads files only, never the AI or the network, so the scorer and its tests can
construct the labelled set with no API key.

Schema (see ``labels.yaml`` for the authoritative comment):

.. code-block:: yaml

    <page_name>:
      url: str
      verdict: will_work | limited | wont_work
      type: str
      tags: [str]
      next_page: str | null
      reviews:
        - prefix: str
          rating: number        # optional
          date: str             # optional
          author: str           # optional
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Directory of this module; ``pages/`` and ``labels.yaml`` sit beside it.
EVAL_DIR = Path(__file__).resolve().parent
PAGES_DIR = EVAL_DIR / "pages"
LABELS_PATH = EVAL_DIR / "labels.yaml"

#: Viability verdicts a page may be labelled with (Requirement 8.1).
VERDICTS: frozenset[str] = frozenset({"will_work", "limited", "wont_work"})

#: Every layout type the seeded evaluation set must cover (Requirement 8.1):
#: structured review data, client-side-rendered pages, plain review lists,
#: reviews without ratings, multi-page listings, a page mixing reviews with Q&A
#: or seller responses, and blocker pages. ``structured_jsonld`` and
#: ``structured_microdata`` together satisfy "Structured Review Data".
REQUIRED_LAYOUT_TAGS: frozenset[str] = frozenset(
    {
        "structured_jsonld",
        "structured_microdata",
        "js_rendered",
        "plain_list",
        "no_ratings",
        "multipage",
        "mixed_qa_seller",
        "blocker",
    }
)


@dataclass(frozen=True)
class ExpectedReview:
    """One hand-checked expected review.

    ``prefix`` is a leading substring of the review's visible text, long enough
    to match the review unambiguously. The field values are what a correct
    extraction should read from the page; a field is ``None`` when the page does
    not carry it (for example ratings on a no-ratings page).
    """

    prefix: str
    rating: float | None = None
    date: str | None = None
    author: str | None = None


@dataclass(frozen=True)
class SelectorSet:
    """A known-good CSS selector set for a page, used to score the offline
    ``selectors`` method without an AI-built plan.

    Mirrors :class:`app.extraction.models.LocatorSelectors`. ``item`` is the
    wrapper selector; the rest are read within each item. Optional per page: a
    page without a clean selector structure (for example a blocker) simply omits
    it and the ``selectors`` method is reported as not-applicable offline.
    """

    item: str
    text: str | None = None
    rating: str | None = None
    date: str | None = None
    author: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class LabeledPage:
    """A labelled evaluation page: its HTML, URL, verdict, tags, and expectations."""

    name: str
    url: str
    verdict: str
    type: str
    tags: tuple[str, ...]
    next_page: str | None
    reviews: tuple[ExpectedReview, ...]
    html: str
    selectors: SelectorSet | None = field(default=None)

    @property
    def is_will_work(self) -> bool:
        """Whether this page is labelled ``will_work`` (used by the thresholds)."""
        return self.verdict == "will_work"


def _parse_review(raw: dict[str, Any], *, page: str) -> ExpectedReview:
    """Parse one expected-review mapping, validating required keys."""
    if "prefix" not in raw or not str(raw["prefix"]).strip():
        raise ValueError(f"Page {page!r}: every expected review needs a non-empty 'prefix'")
    rating = raw.get("rating")
    return ExpectedReview(
        prefix=str(raw["prefix"]),
        rating=float(rating) if rating is not None else None,
        date=str(raw["date"]) if raw.get("date") is not None else None,
        author=str(raw["author"]) if raw.get("author") is not None else None,
    )


def _parse_page(name: str, raw: dict[str, Any]) -> LabeledPage:
    """Parse one page entry and load its HTML from ``pages/<name>/page.html``."""
    verdict = str(raw.get("verdict", "")).strip()
    if verdict not in VERDICTS:
        raise ValueError(
            f"Page {name!r}: verdict {verdict!r} must be one of {sorted(VERDICTS)}"
        )
    url = str(raw.get("url", "")).strip()
    if not url:
        raise ValueError(f"Page {name!r}: a non-empty 'url' is required")

    tags = tuple(str(t) for t in raw.get("tags", []))
    next_page_raw = raw.get("next_page")
    next_page = str(next_page_raw) if next_page_raw is not None else None

    reviews = tuple(_parse_review(r, page=name) for r in raw.get("reviews", []))

    selectors = _parse_selectors(raw.get("selectors"), page=name)

    html_path = PAGES_DIR / name / "page.html"
    if not html_path.exists():
        raise FileNotFoundError(f"Page {name!r}: missing saved HTML at {html_path}")
    html = html_path.read_text(encoding="utf-8")

    return LabeledPage(
        name=name,
        url=url,
        verdict=verdict,
        type=str(raw.get("type", "")),
        tags=tags,
        next_page=next_page,
        reviews=reviews,
        html=html,
        selectors=selectors,
    )


def _parse_selectors(raw: Any, *, page: str) -> SelectorSet | None:
    """Parse an optional known-good selector set for the offline selectors method."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not str(raw.get("item", "")).strip():
        raise ValueError(f"Page {page!r}: 'selectors' needs at least a non-empty 'item'")

    def _opt(key: str) -> str | None:
        value = raw.get(key)
        return str(value) if value is not None else None

    return SelectorSet(
        item=str(raw["item"]),
        text=_opt("text"),
        rating=_opt("rating"),
        date=_opt("date"),
        author=_opt("author"),
        title=_opt("title"),
    )


def load_labeled_pages(labels_path: Path = LABELS_PATH) -> list[LabeledPage]:
    """Load and validate every labelled page from ``labels.yaml``.

    :param labels_path: Path to the labels file (defaults to the one beside this
        module). Overridable so tests can point at a fixture file.
    :returns: The labelled pages in file order, each with its HTML loaded.
    :raises ValueError: A page entry is malformed (bad verdict, missing url, or a
        review without a prefix).
    :raises FileNotFoundError: A page's saved HTML is missing.
    """
    raw = yaml.safe_load(labels_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{labels_path} must be a mapping of page name to label entry")
    return [_parse_page(name, entry) for name, entry in raw.items()]


def covered_tags(pages: list[LabeledPage]) -> set[str]:
    """Return the set of layout tags present across ``pages``."""
    covered: set[str] = set()
    for page in pages:
        covered.update(page.tags)
    return covered


def missing_required_tags(pages: list[LabeledPage]) -> set[str]:
    """Return the required layout tags not covered by the seed (empty when complete)."""
    return set(REQUIRED_LAYOUT_TAGS) - covered_tags(pages)
