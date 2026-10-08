"""Property-based test for the review-analysis metrics stage (task 5).

Property 3: Metrics agree with data.
  For any review set, ``review_count`` SHALL equal the number of stored
  reviews, the rating distribution SHALL sum to the number of rated reviews,
  and the sentiment breakdown SHALL sum to ``review_count``.
  Validates: Requirements 5.1, 5.2

The property is tested against the real stage, ``app.handlers.metrics.
compute_metrics``, over Hypothesis-generated review sets whose ratings vary
across ``None`` (unrated), whole stars, fractional values, and out-of-range
values (so the distribution's rounding/clamping is exercised), paired with
independently generated sentiment labels. No S3 is touched — the computation
is pure, so the property drives ``compute_metrics`` directly (not ``run``).
"""

from __future__ import annotations

from app.extraction.models import VerifiedReview
from app.handlers import metrics
from app.handlers.extraction_stage import CollectedReview
from app.worker.ai.profile import EntityProfile
from app.worker.ai.sentiment import Sentiment
from hypothesis import given
from hypothesis import strategies as st

_SENTIMENTS: list[Sentiment] = ["positive", "neutral", "negative"]


def _collected_review() -> st.SearchStrategy[CollectedReview]:
    """A review with a varied optional rating/date and non-empty text.

    Ratings span ``None``, whole 1–5, fractional (so rounding is exercised), and
    out-of-range values (so clamping is exercised); dates span ``None`` and ISO
    strings, so the "with ratings/dates" and "without" branches are both hit.
    """
    rating = st.one_of(
        st.none(),
        st.integers(min_value=1, max_value=5),
        st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    )
    date = st.one_of(st.none(), st.dates().map(lambda d: d.isoformat()))
    return st.builds(
        lambda text, r, d, page: CollectedReview(
            review=VerifiedReview(text=text, rating=r, date=d),
            source_page=page,
        ),
        st.text(min_size=1, max_size=8),
        rating,
        date,
        st.integers(min_value=0, max_value=10),
    )


@st.composite
def _reviews_and_sentiments(
    draw: st.DrawFn,
) -> tuple[list[CollectedReview], list[Sentiment]]:
    """A review set and a one-label-per-review sentiment list, aligned."""
    reviews = draw(st.lists(_collected_review(), max_size=30))
    sentiments = draw(
        st.lists(st.sampled_from(_SENTIMENTS), min_size=len(reviews), max_size=len(reviews))
    )
    return reviews, sentiments


def _profile() -> EntityProfile:
    return EntityProfile(name="X", confidence="high")


@given(data=_reviews_and_sentiments())
def test_metrics_agree_with_data(data: tuple[list[CollectedReview], list[Sentiment]]) -> None:
    """Property 3: Metrics agree with data.

    review_count equals the stored review count; the rating distribution sums to
    the number of rated reviews; the sentiment breakdown sums to review_count.
    Validates: Requirements 5.1, 5.2
    """
    reviews, sentiments = data

    result = metrics.compute_metrics(
        dataset_id="ds",
        version=1,
        reviews=reviews,
        sentiments=sentiments,
        themes=[],
        profile=_profile(),
        pages=[],
    )
    m = result.metrics

    # review_count equals the number of stored reviews (and the written doc).
    assert m["review_count"] == len(reviews)
    assert len(result.reviews_doc["reviews"]) == len(reviews)

    # The rating distribution sums to the number of rated reviews (None when
    # there are no ratings at all).
    rated = sum(1 for c in reviews if c.review.rating is not None)
    if rated == 0:
        assert m["rating_distribution"] is None
        assert m["avg_rating"] is None
    else:
        assert sum(m["rating_distribution"].values()) == rated

    # The sentiment breakdown sums to review_count.
    assert sum(m["sentiment"].values()) == m["review_count"]
