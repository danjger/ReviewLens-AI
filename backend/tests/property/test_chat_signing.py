"""Property-based test for chat Exchange signing (guardrailed-chat Task 10).

Property 4: Saved history can't be forged.
  For any change to a signed Exchange payload, the save endpoint SHALL refuse
  it.
  Validates: Requirements 5.1, 5.6

Drives the REAL :func:`app.chat.signing.sign_exchange` /
:func:`app.chat.signing.verify_exchange`. For a generated Exchange dict:

  * signing then verifying succeeds;
  * mutating any *signed* field (every key except the excluded ``saved`` /
    ``signature``) makes verification fail — the content can't be forged;
  * flipping the excluded ``saved`` flag keeps verification passing — it is a
    transport status, not signed content.

The generator points directly at the excluded fields that bite: it always offers
``saved`` and ``signature`` alongside the signed fields so the "excluded" and
"signed" branches are both exercised.
"""

from __future__ import annotations

from typing import Any

from app.chat.signing import canonical_payload, sign_exchange, verify_exchange
from hypothesis import assume, given
from hypothesis import strategies as st

_SECRET = "test-signing-secret"

# JSON-serialisable scalar values an Exchange field may hold.
_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1000, max_value=1000),
    st.text(max_size=20),
)

# Field names an Exchange may carry. ``saved`` and ``signature`` are the two the
# signing scheme excludes; the rest are signed content.
_field_name = st.sampled_from(
    [
        "id",
        "dataset_id",
        "data_version",
        "question",
        "answer",
        "scope",
        "conversation_id",
        "asked_at",
    ]
)


@st.composite
def _exchange(draw: st.DrawFn) -> dict[str, Any]:
    """A signable Exchange dict with at least one signed field."""
    keys = draw(st.lists(_field_name, min_size=1, max_size=8, unique=True))
    exchange: dict[str, Any] = {k: draw(_scalar) for k in keys}
    # ``saved`` is an excluded transport flag; include it so flipping it is tested.
    exchange["saved"] = draw(st.booleans())
    return exchange


def _mutate(value: Any) -> Any:  # noqa: ANN401 - any JSON scalar
    """Return a value distinct from *value* (to force a real content change)."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "x"
    if value is None:
        return "was_none"
    return "changed"


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------


@given(exchange=_exchange(), secret=st.sampled_from([_SECRET, "another-secret"]))
def test_signed_exchange_verifies_and_any_content_change_breaks_it(
    exchange: dict[str, Any],
    secret: str,
) -> None:
    """Property 4: Saved history can't be forged.

    A freshly signed Exchange verifies; mutating any signed field breaks
    verification; flipping the excluded ``saved`` flag does not.
    Validates: Requirements 5.1, 5.6
    """
    signed = dict(exchange)
    signed["signature"] = sign_exchange(signed, secret)

    # A faithfully signed payload verifies.
    assert verify_exchange(signed, secret) is True

    # Flipping ``saved`` (excluded from signing) keeps verification passing.
    flipped = dict(signed)
    flipped["saved"] = not bool(signed.get("saved", False))
    assert verify_exchange(flipped, secret) is True
    # ...because the canonical payload is unchanged by ``saved``.
    assert canonical_payload(flipped) == canonical_payload(signed)

    # Mutating any signed field (anything except saved/signature) breaks it.
    for key, value in list(exchange.items()):
        if key in {"saved", "signature"}:
            continue
        forged = dict(signed)
        forged[key] = _mutate(value)
        assert verify_exchange(forged, secret) is False, f"forged field {key!r} still verified"

    # Adding a brand-new signed field also breaks verification.
    with_extra = dict(signed)
    with_extra["injected_field"] = "surprise"
    assert verify_exchange(with_extra, secret) is False


@given(exchange=_exchange())
def test_wrong_secret_or_missing_signature_is_refused(exchange: dict[str, Any]) -> None:
    """A signature from the wrong secret, or no signature, never verifies.

    Validates: Requirements 5.1, 5.6
    """
    signed = dict(exchange)
    signed["signature"] = sign_exchange(signed, _SECRET)

    # Verifying with a different secret fails.
    other = sign_exchange(signed, "different-secret")
    assume(other != signed["signature"])  # HMAC collisions are astronomically unlikely
    assert verify_exchange(signed, "different-secret") is False

    # A missing or empty signature is refused.
    no_sig = {k: v for k, v in signed.items() if k != "signature"}
    assert verify_exchange(no_sig, _SECRET) is False
    assert verify_exchange({**no_sig, "signature": ""}, _SECRET) is False
