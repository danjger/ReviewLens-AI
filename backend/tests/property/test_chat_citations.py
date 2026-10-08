"""Property-based test for chat citation validation (guardrailed-chat Task 10).

Property 1: Citations are always real.
  For any model answer, every saved citation SHALL be a review ID in the
  Corpus, and every saved snippet SHALL match that review.
  Validates: Requirements 2.2, 2.5

Drives the REAL :func:`app.chat.postprocess.process_citations` over an arbitrary
Corpus (a set of review IDs with text/rating/date) and an arbitrary answer that
mixes real ``[r_xxxx]`` citations with fabricated ones. The property asserts
that every surviving citation resolves to a Corpus review, every attached
snippet matches that review's (truncated) text/rating/date, and
``dropped_citations`` equals the number of fabricated citation occurrences — the
guarantee the design states for Property 1.

An independent oracle re-derives the expected survivors, snippets, and drop
count directly from the generated answer and Corpus, so the test pins the rule
rather than restating the implementation.
"""

from __future__ import annotations

import re

from app.chat.corpus import Corpus, CorpusReview, EntityProfile
from app.chat.postprocess import SNIPPET_MAX_CHARS, extract_citation_ids, process_citations
from hypothesis import given
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Review IDs the Corpus can hold. ``r_`` + 1-4 digits is the stable form the
# citation regex matches; a bounded pool keeps real/fake overlap meaningful.
_review_id = st.builds(lambda n: f"r_{n:04d}", st.integers(min_value=0, max_value=400))

# Review text spans empty, short, and well past SNIPPET_MAX_CHARS so the
# snippet-truncation boundary is exercised. Includes unicode so copied text is
# checked for verbatim preservation.
_review_text = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x2FFF),
    min_size=0,
    max_size=SNIPPET_MAX_CHARS + 40,
)

_rating = st.one_of(st.none(), st.integers(min_value=1, max_value=5))
_date = st.one_of(st.none(), st.dates().map(lambda d: d.isoformat()))


@st.composite
def _corpus_and_answer(draw: st.DrawFn) -> tuple[Corpus, str, list[str]]:
    """Build a Corpus plus an answer mixing its IDs with fabricated ones.

    Returns ``(corpus, answer, citation_tokens)`` where ``citation_tokens`` is
    the ordered list of IDs cited in the answer (so the oracle need not re-parse
    free text, though the property also cross-checks via the real extractor).
    """
    # A set of distinct review IDs with their fields.
    ids = draw(st.lists(_review_id, min_size=0, max_size=12, unique=True))
    reviews = [
        CorpusReview(
            id=rid,
            text=draw(_review_text),
            rating=draw(_rating),
            date=draw(_date),
        )
        for rid in ids
    ]
    corpus = Corpus(
        dataset_id="ds",
        version=1,
        entity=EntityProfile(name="Acme CRM", category="software"),
        reviews=tuple(reviews),
        total_review_count=len(reviews),
    )

    # Fabricated IDs: valid ``r_xxxx`` shape but deliberately outside the Corpus
    # (the 500–999 range is disjoint from the 0–400 Corpus-ID range above).
    fabricated = draw(
        st.lists(
            st.builds(lambda n: f"r_{n:04d}", st.integers(min_value=500, max_value=999)),
            min_size=0,
            max_size=6,
        )
    )

    # The citation tokens the answer will contain, in some interleaved order:
    # real IDs (possibly repeated) and fabricated IDs (possibly repeated).
    real_tokens = draw(st.lists(st.sampled_from(ids), max_size=8)) if ids else []
    fake_tokens = draw(st.lists(st.sampled_from(fabricated), max_size=8)) if fabricated else []
    tokens = draw(st.permutations(real_tokens + fake_tokens))

    # Prose fragments between the citations, free of any ``[r_...]`` so they
    # can't accidentally add citations the oracle doesn't know about.
    filler = st.text(alphabet="abcdefg .,", max_size=12)
    parts: list[str] = [draw(filler)]
    for tok in tokens:
        parts.append(f"[{tok}]")
        parts.append(draw(filler))
    answer = "".join(parts)

    return corpus, answer, list(tokens)


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------


@given(_corpus_and_answer())
def test_surviving_citations_are_real_and_snippets_match(
    case: tuple[Corpus, str, list[str]],
) -> None:
    """Property 1: Citations are always real.

    Every surviving citation is a Corpus review ID, each snippet matches that
    review's (truncated) text/rating/date, and ``dropped_citations`` counts
    exactly the fabricated citation occurrences.
    Validates: Requirements 2.2, 2.5
    """
    corpus, answer, _tokens = case
    reviews_by_id = {r.id: r for r in corpus.reviews}

    result = process_citations(answer, corpus)

    # 1) Every surviving citation is a real Corpus ID, with no duplicates.
    assert len(result.citations) == len(set(result.citations))
    for rid in result.citations:
        assert rid in reviews_by_id, f"Survivor {rid!r} is not a Corpus review ID"

    # 2) Snippet keys are exactly the survivors, and each snippet matches its
    #    Corpus review's truncated text / rating / date (never generated).
    assert set(result.snippets) == set(result.citations)
    for rid, snippet in result.snippets.items():
        review = reviews_by_id[rid]
        assert snippet.text == review.text[:SNIPPET_MAX_CHARS]
        assert snippet.rating == review.rating
        assert snippet.date == review.date

    # 3) Independent oracle over the real extractor: survivors are the valid IDs
    #    in first-seen order; drops are every occurrence of a non-Corpus ID.
    extracted = extract_citation_ids(answer)
    expected_survivors: list[str] = []
    expected_dropped = 0
    for rid in extracted:
        if rid in reviews_by_id:
            if rid not in expected_survivors:
                expected_survivors.append(rid)
        else:
            expected_dropped += 1

    assert result.citations == expected_survivors
    assert result.dropped_citations == expected_dropped

    # The regex only ever matches the exact [r_<digits>] form.
    assert all(re.fullmatch(r"r_\d+", rid) for rid in extracted)
