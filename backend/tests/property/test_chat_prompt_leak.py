"""Property-based test for the chat prompt-leak detector (guardrailed-chat Task 10).

Property 5: Prompt leaks are caught.
  For any answer containing a substring of the system prompt of 60 characters or
  more, the saved and shown answer SHALL be the standard decline.
  Validates: Requirement 4.3

Drives the REAL :func:`app.chat.postprocess.enforce_no_prompt_leak` /
:func:`app.chat.postprocess.contains_prompt_leak` against the REAL loaded system
prompt (:func:`app.chat.prompts.load_system_prompt`), normalised exactly the way
the detector normalises it. The property:

  * takes a contiguous window of >= :data:`PROMPT_LEAK_MIN_CHARS` (60) characters
    from the normalised system prompt, embeds it at an arbitrary position inside
    an answer, and asserts the detector reports a leak and replaces the answer
    with :data:`STANDARD_DECLINE`;
  * takes a < 60-character window and wraps it in sentinel characters that never
    appear in the prompt, and asserts it is NOT flagged and the answer is kept.

The 60-character boundary is pinned directly: a window of exactly 60 leaks, a
window of exactly 59 does not. The oracle is independent of the implementation —
it reasons purely from "a run of >= 60 shared normalised characters is a leak".
"""

from __future__ import annotations

from app.chat.postprocess import (
    PROMPT_LEAK_MIN_CHARS,
    STANDARD_DECLINE,
    contains_prompt_leak,
    enforce_no_prompt_leak,
)
from app.chat.prompts import load_system_prompt
from hypothesis import assume, given
from hypothesis import strategies as st


def _normalize(text: str) -> str:
    """Mirror :func:`app.chat.postprocess._normalize` (collapse whitespace runs).

    The detector compares whitespace-normalised strings, so the oracle must
    normalise identically to reason about contiguous overlap lengths.
    """
    return " ".join(text.split())


#: The system prompt normalised the way the detector sees it. Computed once.
_NORM_PROMPT = _normalize(load_system_prompt().text)

#: Sentinel characters that never appear in the (normalised) system prompt, used
#: to wrap a short window so the surrounding answer text can't extend the
#: overlap past the window itself.
_SENTINEL = next(c * 80 for c in ("\u00a7", "\u2042", "Z", "Q") if c not in _NORM_PROMPT)


def _window_at(start: int, length: int) -> str:
    """A ``length``-char window of the normalised prompt that survives re-normalising.

    The detector normalises the *answer* too (:func:`_normalize`), which strips
    leading/trailing whitespace. A window beginning or ending on a space would
    lose that char once embedded and re-normalised, so we take a window whose
    first and last characters are non-space by nudging ``start`` forward and
    extending the end — keeping at least ``length`` contiguous prompt characters.
    """
    end = start + length
    # Advance past any leading space so the window doesn't start on whitespace.
    while start < end and _NORM_PROMPT[start] == " ":
        start += 1
        end += 1
    # Extend to a non-space end so the trailing char can't be stripped.
    while end < len(_NORM_PROMPT) and _NORM_PROMPT[end - 1] == " ":
        end += 1
    return _NORM_PROMPT[start:end]


# ---------------------------------------------------------------------------
# Property 5 — a 60+ char window of the prompt is always caught
# ---------------------------------------------------------------------------


@given(
    data=st.data(),
    prefix=st.text(alphabet="abcdefg .,", max_size=30),
    suffix=st.text(alphabet="abcdefg .,", max_size=30),
    window_len=st.integers(min_value=PROMPT_LEAK_MIN_CHARS, max_value=140),
)
def test_sixty_char_prompt_window_is_always_caught(
    data: st.DataObject,
    prefix: str,
    suffix: str,
    window_len: int,
) -> None:
    """Property 5: Prompt leaks are caught.

    Any answer embedding a contiguous >= 60-character window of the system
    prompt is detected and replaced with the standard decline.
    Validates: Requirement 4.3
    """
    window_len = min(window_len, len(_NORM_PROMPT))
    assume(window_len >= PROMPT_LEAK_MIN_CHARS)

    start = data.draw(
        st.integers(min_value=0, max_value=len(_NORM_PROMPT) - window_len),
        label="window_start",
    )
    window = _window_at(start, window_len)
    # The surviving window still carries a 60+ char contiguous prompt run.
    assume(len(window) >= PROMPT_LEAK_MIN_CHARS)

    answer = f"{prefix}{window}{suffix}"

    # The detector flags the leak...
    assert contains_prompt_leak(answer) is True

    # ...and the enforcer replaces the answer with the standard decline.
    result = enforce_no_prompt_leak(answer)
    assert result.leaked is True
    assert result.answer == STANDARD_DECLINE


# ---------------------------------------------------------------------------
# Property 5 (boundary) — a sub-60 char window is never caught
# ---------------------------------------------------------------------------


@given(
    data=st.data(),
    window_len=st.integers(min_value=1, max_value=PROMPT_LEAK_MIN_CHARS - 1),
)
def test_short_prompt_window_is_never_caught(
    data: st.DataObject,
    window_len: int,
) -> None:
    """Property 5 (boundary): a < 60-character overlap is not a leak.

    A window shorter than :data:`PROMPT_LEAK_MIN_CHARS`, wrapped in sentinel
    characters absent from the prompt (so the surrounding text can't lengthen
    the shared run), is kept unchanged.
    Validates: Requirement 4.3
    """
    start = data.draw(
        st.integers(min_value=0, max_value=len(_NORM_PROMPT) - window_len),
        label="window_start",
    )
    window = _NORM_PROMPT[start : start + window_len]

    # Sentinels never appear in the prompt, so the longest run of prompt text in
    # the answer is exactly ``window`` (< 60 chars) — below the threshold.
    answer = f"{_SENTINEL}{window}{_SENTINEL}"

    assert contains_prompt_leak(answer) is False
    result = enforce_no_prompt_leak(answer)
    assert result.leaked is False
    assert result.answer == answer


# ---------------------------------------------------------------------------
# Property 5 (exact boundary) — 60 leaks, 59 does not
# ---------------------------------------------------------------------------


@given(
    start=st.integers(min_value=0, max_value=len(_NORM_PROMPT) - PROMPT_LEAK_MIN_CHARS),
)
def test_exact_threshold_boundary(start: int) -> None:
    """Property 5 (exact boundary): a 60-char window leaks, a 59-char one does not.

    Pins the strict ``>= 60`` threshold so a ``> 60`` or ``>= 59`` regression is
    caught. The 59-char window is sentinel-wrapped so no neighbouring text can
    push it to 60.
    Validates: Requirement 4.3
    """
    # A window of >= 60 non-space-bounded prompt chars; sentinel-wrapped so the
    # longest prompt run in the answer is exactly its own length.
    window = _window_at(start, PROMPT_LEAK_MIN_CHARS)
    assume(len(window) >= PROMPT_LEAK_MIN_CHARS)
    answer_leak = f"{_SENTINEL}{window}{_SENTINEL}"
    assert contains_prompt_leak(answer_leak) is True
    assert enforce_no_prompt_leak(answer_leak).answer == STANDARD_DECLINE

    # Trim to 59 contiguous prompt chars: now below the threshold. Sentinel
    # wrapping stops neighbouring text from pushing the run back to 60.
    only_59 = window[: PROMPT_LEAK_MIN_CHARS - 1]
    assert len(only_59) == PROMPT_LEAK_MIN_CHARS - 1
    answer_59 = f"{_SENTINEL}{only_59}{_SENTINEL}"
    assert contains_prompt_leak(answer_59) is False
    assert enforce_no_prompt_leak(answer_59).leaked is False
