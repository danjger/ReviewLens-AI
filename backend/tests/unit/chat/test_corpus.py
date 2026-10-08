"""Unit tests for the chat Corpus loader (``app.chat.corpus``), Task 1.2.

Covers:

- Loading ``reviews/v{n}.json`` into a :class:`Corpus` (entity + reviews),
  reading the object through ``storage.s3`` with a key from ``storage.keys``.
- The warm in-memory cache keyed by ``(dataset_id, version)``: a second load
  serves from memory without touching S3, and ``reset_corpus_cache`` clears it;
  a different version is a different key (no stale hit).
- The ``CHAT_CORPUS_TOKEN_BUDGET`` truncation: reviews over budget keep the most
  recent and attach a note saying the answer covers N of M reviews.

S3 (``corpus.s3.get_text``) is faked at the module boundary and the token budget
is set via ``get_settings`` monkeypatching, so the tests run offline with no AWS
and no AI call.

_Validates: Requirement 2.1._
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.chat import corpus
from app.core.config import get_settings
from app.storage import keys

_DS = "11111111-1111-1111-1111-111111111111"


def _review(
    rid: str,
    *,
    text: str = "A fine product.",
    rating: int = 5,
    date: str | None = "2026-01-01",
    sentiment: str = "positive",
    source_page: int = 1,
) -> dict[str, Any]:
    return {
        "id": rid,
        "text": text,
        "rating": rating,
        "date": date,
        "author": "A. Buyer",
        "title": "Nice",
        "sentiment": sentiment,
        "source_page": source_page,
    }


def _doc(reviews: list[dict[str, Any]], *, version: int = 2) -> dict[str, Any]:
    return {
        "dataset_id": _DS,
        "version": version,
        "generated_at": "2026-01-02T00:00:00+00:00",
        "entity": {
            "name": "Acme CRM",
            "category": "software",
            "description": "A CRM tool.",
            "confidence": "high",
        },
        "pages": [],
        "reviews": reviews,
    }


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake ``s3.get_text`` and give a generous default token budget.

    ``state["objects"]`` maps an S3 key to its JSON text. ``state["reads"]``
    records every key the loader read, so a test can assert the cache avoided a
    second S3 read.
    """
    state: dict[str, Any] = {"objects": {}, "reads": []}

    def _get_text(key: str, *, encoding: str = "utf-8") -> str:
        state["reads"].append(key)
        return str(state["objects"][key])

    monkeypatch.setattr(corpus.s3, "get_text", _get_text)

    # Default: a large budget so nothing truncates unless a test lowers it.
    monkeypatch.setenv("CHAT_CORPUS_TOKEN_BUDGET", "150000")
    get_settings.cache_clear()

    corpus.reset_corpus_cache()
    yield state
    corpus.reset_corpus_cache()
    get_settings.cache_clear()


def _store(state: dict[str, Any], doc: dict[str, Any], *, version: int = 2) -> str:
    key = keys.dataset_reviews(_DS, version)
    state["objects"][key] = json.dumps(doc)
    return key


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_load_corpus_reads_entity_and_reviews(wiring: dict[str, Any]) -> None:
    """The Corpus carries the entity profile and the reviews from the file."""
    _store(wiring, _doc([_review("r_0001"), _review("r_0002")]))

    loaded = corpus.load_corpus(_DS, 2)

    assert loaded.dataset_id == _DS
    assert loaded.version == 2
    assert loaded.entity.name == "Acme CRM"
    assert loaded.entity.category == "software"
    assert loaded.entity.confidence == "high"
    assert {r.id for r in loaded.reviews} == {"r_0001", "r_0002"}
    assert loaded.total_review_count == 2
    assert loaded.truncation_note is None
    assert loaded.is_truncated is False


def test_load_corpus_uses_storage_keys_key(wiring: dict[str, Any]) -> None:
    """The object is read with the key from ``storage.keys``, never hand-built."""
    _store(wiring, _doc([_review("r_0001")]))

    corpus.load_corpus(_DS, 2)

    assert wiring["reads"] == [keys.dataset_reviews(_DS, 2)]


def test_load_corpus_handles_empty_and_missing_entity(wiring: dict[str, Any]) -> None:
    """A document with no reviews and a bare entity still loads."""
    doc = _doc([])
    doc["entity"] = {"name": "Acme CRM"}
    _store(wiring, doc)

    loaded = corpus.load_corpus(_DS, 2)

    assert loaded.reviews == ()
    assert loaded.total_review_count == 0
    assert loaded.entity.name == "Acme CRM"
    assert loaded.entity.category is None


# ---------------------------------------------------------------------------
# Warm cache keyed by (dataset_id, version)
# ---------------------------------------------------------------------------


def test_second_load_is_served_from_cache(wiring: dict[str, Any]) -> None:
    """A repeat load of the same (id, version) does not re-read S3."""
    _store(wiring, _doc([_review("r_0001")]))

    first = corpus.load_corpus(_DS, 2)
    second = corpus.load_corpus(_DS, 2)

    assert first is second  # same cached object
    assert wiring["reads"] == [keys.dataset_reviews(_DS, 2)]  # read exactly once


def test_reset_cache_forces_reread(wiring: dict[str, Any]) -> None:
    """After ``reset_corpus_cache`` the next load reads S3 again."""
    _store(wiring, _doc([_review("r_0001")]))

    corpus.load_corpus(_DS, 2)
    corpus.reset_corpus_cache()
    corpus.load_corpus(_DS, 2)

    assert wiring["reads"] == [keys.dataset_reviews(_DS, 2)] * 2


def test_cache_is_keyed_by_version(wiring: dict[str, Any]) -> None:
    """A different version is a different key, so no stale cache hit occurs."""
    _store(wiring, _doc([_review("r_0001")], version=2), version=2)
    _store(wiring, _doc([_review("r_0001"), _review("r_0002")], version=3), version=3)

    v2 = corpus.load_corpus(_DS, 2)
    v3 = corpus.load_corpus(_DS, 3)

    assert v2.version == 2
    assert v3.version == 3
    assert v2.total_review_count == 1
    assert v3.total_review_count == 2
    assert wiring["reads"] == [
        keys.dataset_reviews(_DS, 2),
        keys.dataset_reviews(_DS, 3),
    ]


# ---------------------------------------------------------------------------
# Token-budget truncation note
# ---------------------------------------------------------------------------


def test_budget_truncation_keeps_most_recent_with_note(
    wiring: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over budget, the newest reviews are kept and a 'N of M' note is attached."""
    # Each review's text is ~400 chars -> ~100 tokens. A 150-token budget fits
    # one review, so of three only the most recent (latest date) survives.
    long_text = "x" * 400
    reviews = [
        _review("r_old", text=long_text, date="2025-01-01"),
        _review("r_mid", text=long_text, date="2025-06-01"),
        _review("r_new", text=long_text, date="2026-01-01"),
    ]
    _store(wiring, _doc(reviews))

    monkeypatch.setenv("CHAT_CORPUS_TOKEN_BUDGET", "150")
    get_settings.cache_clear()
    corpus.reset_corpus_cache()

    loaded = corpus.load_corpus(_DS, 2)

    assert loaded.total_review_count == 3
    assert [r.id for r in loaded.reviews] == ["r_new"]
    assert loaded.is_truncated is True
    assert loaded.truncation_note is not None
    assert "1" in loaded.truncation_note
    assert "3" in loaded.truncation_note


def test_within_budget_has_no_note(wiring: dict[str, Any]) -> None:
    """When everything fits, there is no truncation note."""
    _store(wiring, _doc([_review("r_0001"), _review("r_0002")]))

    loaded = corpus.load_corpus(_DS, 2)

    assert loaded.truncation_note is None
    assert len(loaded.reviews) == 2


def test_single_oversized_review_is_still_kept(
    wiring: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lone review bigger than the whole budget is retained (never zero)."""
    _store(wiring, _doc([_review("r_0001", text="y" * 10_000)]))

    monkeypatch.setenv("CHAT_CORPUS_TOKEN_BUDGET", "10")
    get_settings.cache_clear()
    corpus.reset_corpus_cache()

    loaded = corpus.load_corpus(_DS, 2)

    assert len(loaded.reviews) == 1
    assert loaded.truncation_note is None  # nothing was dropped


def test_reviews_sorted_newest_first(wiring: dict[str, Any]) -> None:
    """Reviews are ordered newest first so 'keep most recent' is well defined."""
    reviews = [
        _review("r_a", date="2024-05-01"),
        _review("r_b", date="2026-05-01"),
        _review("r_c", date="2025-05-01"),
    ]
    _store(wiring, _doc(reviews))

    loaded = corpus.load_corpus(_DS, 2)

    assert [r.id for r in loaded.reviews] == ["r_b", "r_c", "r_a"]


def test_review_without_date_sorts_last(wiring: dict[str, Any]) -> None:
    """A dateless review never displaces a dated, more recent one."""
    reviews = [
        _review("r_dated", date="2025-01-01"),
        _review("r_nodate", date=None),
    ]
    _store(wiring, _doc(reviews))

    loaded = corpus.load_corpus(_DS, 2)

    assert [r.id for r in loaded.reviews] == ["r_dated", "r_nodate"]
