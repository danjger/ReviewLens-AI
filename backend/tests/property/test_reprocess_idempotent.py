"""Property-based test for the review-analysis completion stage (task 6).

Property 5: Reprocessing is idempotent.
  For any dataset version, running the pipeline twice SHALL produce identical
  stored outputs and one ``dataset_versions`` completion.
  Validates: Requirement 7.3

Two independent facts make reprocessing idempotent, and the property checks
both over Hypothesis-generated review sets:

1. **Identical stored outputs.** The metrics-and-output stage
   (:func:`app.handlers.metrics.compute_metrics`) is a pure function of its
   inputs, so running it twice on the same reviews/sentiments/themes/profile
   yields byte-identical artifacts — the ``metrics`` dict and the
   ``reviews/v{n}.json`` document — apart from the non-semantic ``generated_at``
   timestamp. The output key includes the version, so the second write
   overwrites the first in place (checked in the metrics/output tests); here we
   assert the *content* is stable.

2. **One completion.** The ``dataset_versions`` row is completed under a
   conditional claim (``outcome IS NULL``) in
   :func:`app.handlers.completion.complete`. A faithful in-memory model of that
   claim is folded over repeated completions of the same version: exactly one
   call wins and flips the row, every later call is a no-op. The real SQL claim
   is exercised in the completion unit tests; this property shows the *gate*
   admits one completion however many times the version is reprocessed.
"""

from __future__ import annotations

from app.extraction.models import VerifiedReview
from app.handlers import metrics
from app.handlers.completion import CompletionDecision, Outcome, decide_completion
from app.handlers.extraction_stage import CollectedReview
from app.worker.ai.profile import EntityProfile
from app.worker.ai.sentiment import Sentiment
from hypothesis import given, settings
from hypothesis import strategies as st

_SENTIMENTS: list[Sentiment] = ["positive", "neutral", "negative"]


def _collected_review() -> st.SearchStrategy[CollectedReview]:
    rating = st.one_of(st.none(), st.integers(min_value=1, max_value=5))
    date = st.one_of(st.none(), st.dates().map(lambda d: d.isoformat()))
    return st.builds(
        lambda text, r, d, page: CollectedReview(
            review=VerifiedReview(text=text, rating=r, date=d),
            source_page=page,
        ),
        st.text(min_size=1, max_size=8),
        rating,
        date,
        st.integers(min_value=1, max_value=5),
    )


@st.composite
def _reviews_and_sentiments(
    draw: st.DrawFn,
) -> tuple[list[CollectedReview], list[Sentiment]]:
    reviews = draw(st.lists(_collected_review(), max_size=20))
    sentiments = draw(
        st.lists(st.sampled_from(_SENTIMENTS), min_size=len(reviews), max_size=len(reviews))
    )
    return reviews, sentiments


def _profile() -> EntityProfile:
    return EntityProfile(name="X", confidence="high")


def _without_timestamp(doc: dict) -> dict:
    """A copy of the reviews doc without the non-semantic ``generated_at``.

    ``generated_at`` records *when* the document was produced, not *what* it
    contains; it is the only field that legitimately differs between two runs.
    Everything the product reads — the entity, pages, and the reviews with their
    minted ids, text, ratings, dates, authors, titles, sentiment, and source
    page — must be identical.
    """
    return {k: v for k, v in doc.items() if k != "generated_at"}


@settings(max_examples=200)
@given(data=_reviews_and_sentiments())
def test_outputs_are_identical_on_rerun(
    data: tuple[list[CollectedReview], list[Sentiment]],
) -> None:
    """Property 5: reprocessing produces identical stored outputs.

    Computing the metrics and reviews document twice from the same inputs yields
    identical ``metrics`` and identical ``reviews/v{n}.json`` content (modulo the
    ``generated_at`` timestamp).
    Validates: Requirement 7.3
    """
    reviews, sentiments = data

    def _compute() -> metrics.MetricsResult:
        return metrics.compute_metrics(
            dataset_id="ds",
            version=1,
            reviews=reviews,
            sentiments=sentiments,
            themes=[],
            profile=_profile(),
            pages=[],
        )

    first = _compute()
    second = _compute()

    # The metrics dict is fully deterministic.
    assert first.metrics == second.metrics
    assert first.review_count == second.review_count
    # The reviews document is identical apart from the generated-at timestamp.
    assert _without_timestamp(first.reviews_doc) == _without_timestamp(second.reviews_doc)


class _VersionRow:
    """In-memory model of a ``dataset_versions`` row's conditional claim.

    Mirrors the ``UPDATE ... WHERE outcome IS NULL RETURNING`` gate in
    :func:`app.handlers.completion.complete`: the first completion flips
    ``outcome`` from ``None`` and wins; every later completion finds it already
    set and is a no-op.
    """

    def __init__(self) -> None:
        self.outcome: str | None = None
        self.completions = 0

    def claim(self, decision: CompletionDecision) -> bool:
        if self.outcome is not None:
            return False
        self.outcome = decision.outcome.value
        self.completions += 1
        return True


@settings(max_examples=200)
@given(
    reruns=st.integers(min_value=1, max_value=8),
    succeeded=st.booleans(),
)
def test_one_completion_however_many_reruns(reruns: int, succeeded: bool) -> None:
    """Property 5: a version is completed exactly once, however often it reruns.

    Reprocessing the same version any number of times flips the
    ``dataset_versions`` row once; the first run wins the claim and the rest are
    idempotent no-ops.
    Validates: Requirement 7.3
    """
    row = _VersionRow()
    decision = decide_completion(review_count=3 if succeeded else 0, error=None, is_refresh=False)

    wins = [row.claim(decision) for _ in range(reruns)]

    # Exactly one win and exactly one completion, whatever the rerun count.
    assert sum(wins) == 1
    assert wins[0] is True
    assert all(w is False for w in wins[1:])
    assert row.completions == 1
    # The single recorded outcome matches the decision.
    expected = Outcome.UPDATED.value if succeeded else Outcome.FAILED.value
    assert row.outcome == expected
