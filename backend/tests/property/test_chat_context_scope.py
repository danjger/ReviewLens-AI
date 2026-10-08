"""Property-based test for chat context scoping (guardrailed-chat Task 10).

Property 2: Context is scoped.
  For any shared history, the messages sent to the model SHALL include at most 6
  Exchanges, all with the asking tab's ``conversation_id`` and the current data
  version.
  Validates: Requirements 2.6, 9.6

Drives the REAL :func:`app.chat.assembly.select_history` over a mixed shared
history (varied ``conversation_id`` / ``data_version`` / ``asked_at``). The
property asserts the selection is at most :data:`MAX_HISTORY_EXCHANGES` (6), that
every selected Exchange matches the asking ``conversation_id`` AND the current
``data_version``, that the result is ordered oldest-first by ``asked_at``, and
that it is exactly the most-recent window of the eligible Exchanges (an
independent oracle re-derives it). The generator sits the eligible count right
on the window boundary (0..8 matching Exchanges) where the ``<= 6`` cap bites.
"""

from __future__ import annotations

from app.chat.assembly import MAX_HISTORY_EXCHANGES, PriorExchange, select_history
from hypothesis import given
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A small pool of conversation IDs and data versions so the asking tab's ID and
# the active version collide with other Exchanges often enough to matter.
_conversation_id = st.sampled_from(["conv-a", "conv-b", "conv-c"])
_data_version = st.integers(min_value=1, max_value=3)

# Distinct ISO-ish timestamps as plain sortable strings. Using zero-padded
# integers keeps lexicographic order equal to chronological order, matching how
# the real key prefix sorts, while letting ties occur.
_asked_at = st.integers(min_value=0, max_value=40).map(lambda n: f"2026-01-01T00:00:{n:02d}Z")


@st.composite
def _prior_exchange(draw: st.DrawFn) -> PriorExchange:
    return PriorExchange(
        question=draw(st.text(max_size=20)),
        answer=draw(st.text(max_size=40)),
        conversation_id=draw(_conversation_id),
        data_version=draw(_data_version),
        asked_at=draw(_asked_at),
    )


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------


@given(
    history=st.lists(_prior_exchange(), min_size=0, max_size=20),
    conversation_id=_conversation_id,
    data_version=_data_version,
)
def test_selected_history_is_scoped_and_bounded(
    history: list[PriorExchange],
    conversation_id: str,
    data_version: int,
) -> None:
    """Property 2: Context is scoped.

    The selected history is <= 6 Exchanges, all with the asking tab's
    ``conversation_id`` and the current ``data_version``, and is the
    most-recent window ordered oldest-first.
    Validates: Requirements 2.6, 9.6
    """
    selected = select_history(
        history,
        conversation_id=conversation_id,
        data_version=data_version,
    )

    # 1) At most the context window (design: "the last 6 Exchanges").
    assert len(selected) <= MAX_HISTORY_EXCHANGES

    # 2) Every selected Exchange matches BOTH the asking conversation and the
    #    current data version — no other conversation and no older version leak.
    for ex in selected:
        assert ex.conversation_id == conversation_id
        assert ex.data_version == data_version

    # 3) Ordered oldest-first by asked_at.
    asked = [ex.asked_at for ex in selected]
    assert asked == sorted(asked)

    # 4) Independent oracle: the eligible Exchanges are those matching both
    #    filters; the selection is the most-recent MAX_HISTORY_EXCHANGES of them,
    #    oldest-first. (select_history sorts by asked_at; mirror that here.)
    eligible = [
        ex
        for ex in history
        if ex.conversation_id == conversation_id and ex.data_version == data_version
    ]
    eligible_sorted = sorted(eligible, key=lambda ex: ex.asked_at)
    expected = eligible_sorted[-MAX_HISTORY_EXCHANGES:]

    assert selected == expected

    # 5) The cap only drops the oldest overflow: when there are more eligible
    #    than the window, exactly the window size is kept.
    if len(eligible) >= MAX_HISTORY_EXCHANGES:
        assert len(selected) == MAX_HISTORY_EXCHANGES
    else:
        assert len(selected) == len(eligible)
