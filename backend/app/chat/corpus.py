"""Corpus loading for the guardrailed chat (Task 1.2).

The **Corpus** is the current data version's Normalized Reviews plus the Entity
Profile — exactly the ``reviews/v{n}.json`` document that review-analysis writes
(design "Data Models" there; glossary of ``guardrailed-chat``). The chat uses
*full-context grounding*: the whole Corpus goes into one cached prompt block so
the model can see every review and counts can be exact.

This module is the single seam that turns a ``(dataset_id, active_version)`` pair
into a :class:`Corpus` ready for message assembly (Task 1.3):

- It reads the immutable ``reviews/v{n}.json`` object through
  :func:`app.storage.s3.get_text`, with the key built by
  :func:`app.storage.keys.dataset_reviews` — this module never assembles an S3
  key by hand (steering: every S3 key comes from ``storage.keys``).
- It keeps a **warm in-memory cache keyed by ``(dataset_id, version)``**. That
  object is written once per version and never mutated (a refresh is a new
  version, i.e. a new key), so a cached entry can never go stale. The cache is a
  performance aid only: a cold process re-reads from S3 and gets the same bytes,
  so correctness never depends on process memory (steering: stateless services).
  This mirrors the read-through cache already used by
  :mod:`app.datasets.summary`.
- When the Corpus would exceed ``CHAT_CORPUS_TOKEN_BUDGET`` (config, default
  150k; never a literal), it keeps the **most recent** reviews that fit the
  budget and records a truncation note saying the answer covers *N of M*
  reviews (design "Error Handling": "Corpus larger than
  ``CHAT_CORPUS_TOKEN_BUDGET``"). With ``MAX_REVIEWS=1000`` this is rare (only
  very long reviews), but the budget is a hard safety limit.

Token sizing is a cheap deterministic estimate (~4 characters per token, which
lands in the design's 100–150 tokens-per-review range) rather than an AI call,
so loading the Corpus never costs a model round-trip. The reviews file is sorted
newest-first here so "keep the most recent" has a defined meaning and the
truncation is deterministic for tests.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.storage import keys, s3

#: Characters-per-token divisor for the deterministic size estimate. Four is the
#: usual rule of thumb for English text and, at the design's ~400–600 characters
#: per review, yields ~100–150 tokens per review — matching the design's own
#: estimate. Using a fixed divisor (rather than an AI token count) keeps Corpus
#: loading free and offline; the budget it feeds is only a safety limit.
_CHARS_PER_TOKEN: int = 4


@dataclass(frozen=True, slots=True)
class CorpusReview:
    """One Normalized Review as the chat consumes it.

    Carries only the fields the model is shown and that citations resolve
    against — ``id`` (the stable ``r_0001`` form), ``rating``, ``date``, and the
    review ``text`` (copied from the source page, never generated). ``author``,
    ``title``, ``sentiment``, and ``source_page`` are retained from the reviews
    file so later tasks (citation snippets, suggestions) can read them without a
    second load, but they are optional.
    """

    id: str
    text: str
    rating: int | None = None
    date: str | None = None
    author: str | None = None
    title: str | None = None
    sentiment: str | None = None
    source_page: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CorpusReview:
        """Build a :class:`CorpusReview` from one entry of the reviews file."""
        return cls(
            id=str(raw["id"]),
            text=str(raw.get("text", "")),
            rating=raw.get("rating"),
            date=raw.get("date"),
            author=raw.get("author"),
            title=raw.get("title"),
            sentiment=raw.get("sentiment"),
            source_page=raw.get("source_page"),
        )

    @property
    def estimated_tokens(self) -> int:
        """A cheap token estimate for this review's shown fields.

        Counts the characters the model actually sees (id, rating, date, and
        text — the JSON-line form the ``<reviews>`` block uses) divided by
        :data:`_CHARS_PER_TOKEN`, with a floor of 1 so an empty review is not
        free. This never calls the AI.
        """
        shown = f"{self.id}{self.rating}{self.date}{self.text}"
        return max(1, len(shown) // _CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class EntityProfile:
    """The identified entity the dataset is about (design ``entity`` object).

    Mirrors ``reviews/v{n}.json``'s ``entity`` block
    (``{name, category, description, confidence}``) and fills the system
    prompt's SCOPE placeholders during message assembly (Task 1.3).
    """

    name: str
    category: str | None = None
    description: str | None = None
    confidence: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EntityProfile:
        return cls(
            name=str(raw.get("name", "")),
            category=raw.get("category"),
            description=raw.get("description"),
            confidence=raw.get("confidence"),
        )


@dataclass(frozen=True, slots=True)
class Corpus:
    """A dataset version's reviews plus entity profile, ready for grounding.

    :ivar dataset_id: The dataset the Corpus belongs to.
    :ivar version: The data version these reviews come from (the dataset's
        ``active_version`` at load time).
    :ivar entity: The identified :class:`EntityProfile`.
    :ivar reviews: The reviews included in the prompt, newest first, already
        trimmed to :data:`~app.core.config.Settings.chat_corpus_token_budget`.
    :ivar total_review_count: How many reviews the version actually has (``M``),
        before any truncation — so callers can report "N of M".
    :ivar estimated_tokens: The estimated token size of :attr:`reviews`.
    :ivar truncation_note: ``None`` when every review fit the budget; otherwise a
        human-readable note that the answer covers *N of M* reviews.
    """

    dataset_id: str
    version: int
    entity: EntityProfile
    reviews: tuple[CorpusReview, ...]
    total_review_count: int
    estimated_tokens: int = 0
    truncation_note: str | None = field(default=None)

    @property
    def is_truncated(self) -> bool:
        """True when the budget dropped at least one review."""
        return len(self.reviews) < self.total_review_count


# ---------------------------------------------------------------------------
# Warm in-memory cache, keyed by (dataset_id, version)
# ---------------------------------------------------------------------------
#
# A performance aid, not correctness-critical state (steering: stateless
# services). ``reviews/v{n}.json`` is write-once per version — a refresh is a
# *new* version, hence a new key — so a cached entry can never go stale. A cold
# process simply re-reads the identical object from S3. A lock keeps concurrent
# requests in the same process consistent, matching ``app.datasets.summary``.
_corpus_cache: dict[tuple[str, int], Corpus] = {}
_corpus_cache_lock = threading.Lock()


def reset_corpus_cache() -> None:
    """Clear the warm Corpus cache.

    For tests that swap S3 contents between cases. Production never needs this
    because a given ``(dataset_id, version)`` object is immutable.
    """
    with _corpus_cache_lock:
        _corpus_cache.clear()


def _sort_newest_first(reviews: list[CorpusReview]) -> list[CorpusReview]:
    """Return *reviews* ordered newest first.

    "Keep the most recent reviews within budget" (design "Error Handling") needs
    a defined order. Reviews are sorted by their ``date`` descending; entries
    without a parseable date sort last (treated as oldest) so a missing date
    never displaces a dated, more recent review. The sort is stable, so reviews
    sharing a date keep their stored order.
    """

    # ISO-8601 date strings (``YYYY-MM-DD``) sort correctly lexicographically.
    # An empty string sorts before any real date, so a missing date must map to
    # a value that sorts *after* real dates under reverse=True; use "" and
    # invert via a (has_date, date) key.
    def key(review: CorpusReview) -> tuple[int, str]:
        has_date = 1 if review.date else 0
        return (has_date, review.date or "")

    return sorted(reviews, key=key, reverse=True)


def _apply_budget(
    reviews: list[CorpusReview], *, budget_tokens: int
) -> tuple[list[CorpusReview], int, str | None]:
    """Keep the most-recent reviews that fit *budget_tokens*.

    *reviews* must already be ordered newest first. Returns the kept reviews,
    their estimated token total, and a truncation note (``None`` when nothing was
    dropped). A single review larger than the whole budget is still kept (never
    answer with zero reviews); the note then reflects the one review retained.
    """
    total = len(reviews)
    kept: list[CorpusReview] = []
    used = 0
    for review in reviews:
        cost = review.estimated_tokens
        if kept and used + cost > budget_tokens:
            break
        kept.append(review)
        used += cost

    if len(kept) == total:
        return kept, used, None

    note = (
        f"This answer covers the {len(kept)} most recent of {total} reviews; "
        f"older reviews were omitted to fit the context budget."
    )
    return kept, used, note


def _parse_corpus(dataset_id: str, version: int, doc: dict[str, Any]) -> Corpus:
    """Build a :class:`Corpus` from a parsed ``reviews/v{n}.json`` document.

    Pure (no S3, no cache) so the ordering and budget logic are unit-testable
    offline. The token budget comes from config (``chat_corpus_token_budget``),
    never a literal.
    """
    entity = EntityProfile.from_dict(doc.get("entity", {}) or {})
    raw_reviews = doc.get("reviews", []) or []
    reviews = [CorpusReview.from_dict(r) for r in raw_reviews]
    ordered = _sort_newest_first(reviews)

    budget = get_settings().chat_corpus_token_budget
    kept, used, note = _apply_budget(ordered, budget_tokens=budget)

    return Corpus(
        dataset_id=dataset_id,
        version=version,
        entity=entity,
        reviews=tuple(kept),
        total_review_count=len(reviews),
        estimated_tokens=used,
        truncation_note=note,
    )


def load_corpus(dataset_id: str, version: int) -> Corpus:
    """Load a dataset version's Corpus, via the warm cache then S3.

    Reads ``datasets/{id}/reviews/v{n}.json`` (key from
    :func:`app.storage.keys.dataset_reviews`) on a cache miss, parses it into a
    :class:`Corpus` (newest-first, trimmed to ``CHAT_CORPUS_TOKEN_BUDGET`` with a
    truncation note when needed), and caches it under ``(dataset_id, version)``.

    :param dataset_id: The dataset to load.
    :param version: The data version to load — the caller passes the dataset's
        ``active_version``; the chat answers from that version only.
    :returns: The ready-to-ground :class:`Corpus`.
    :raises botocore.exceptions.ClientError: Propagated from S3 (e.g.
        ``NoSuchKey``) when the object is absent. Chat availability (an
        ``active_version`` set and the dataset not archived) is enforced by the
        endpoint before this is called, so a missing object here is an invariant
        breach rather than a normal state.
    """
    cache_key = (dataset_id, version)
    with _corpus_cache_lock:
        cached = _corpus_cache.get(cache_key)
    if cached is not None:
        return cached

    key = keys.dataset_reviews(dataset_id, version)
    doc: dict[str, Any] = json.loads(s3.get_text(key))
    corpus = _parse_corpus(dataset_id, version, doc)

    with _corpus_cache_lock:
        _corpus_cache[cache_key] = corpus
    return corpus
