"""HMAC signing of saved chat Exchange payloads (guardrailed-chat Task 4.3).

The chat service saves each answered Exchange to S3 and streams the same
Exchange to the browser in the terminal ``done`` SSE event. If the save fails,
the browser keeps the Exchange and can replay it to the retry-save endpoint
(``POST /datasets/{id}/chat/save``, Task 5.2). Because the app has no sign-in,
anything the browser can replay a visitor could also *forge* — so the service
signs the Exchange with a server secret and the save endpoint refuses any
payload whose signature doesn't match (design "Endpoints"; Correctness
Property 4: "Saved history can't be forged").

Canonical signing scheme (Task 5.2 MUST match this)
---------------------------------------------------
The signature covers every Exchange field **except** the two that are not part
of the Exchange's identity:

- ``signature`` — the field this value is written into; signing it would be
  circular.
- ``saved`` — a transport/status flag, not Exchange content. It is ``False`` in
  the ``done`` event when the first save failed and becomes ``True`` once the
  Exchange is persisted, so it must not change the signature (the browser
  replays the *same* payload it received, and the retry endpoint recomputes the
  signature over the content, ignoring ``saved``).

The signed message is the canonical JSON of the remaining fields:

    json.dumps(fields, sort_keys=True, separators=(",", ":"),
               ensure_ascii=False, default=str)

Sorting keys and using the compact separators make the serialization stable and
independent of dict insertion order, so signing and verifying produce the same
bytes regardless of how the payload was built or round-tripped through JSON.
The HMAC is SHA-256 over the UTF-8 bytes of that string, hex-encoded.

Verification uses :func:`hmac.compare_digest` for a constant-time comparison, so
a verifier never leaks where a forged signature first diverges.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

#: Exchange keys excluded from the signed content (see the module docstring).
_EXCLUDED_FIELDS = frozenset({"signature", "saved"})


def canonical_payload(exchange: dict[str, Any]) -> str:
    """Return the canonical JSON string that the signature is computed over.

    Drops the non-identity fields (:data:`_EXCLUDED_FIELDS`) and serialises the
    rest with sorted keys and compact separators, so the result depends only on
    the Exchange's content and not on dict ordering or whitespace.
    """
    fields = {k: v for k, v in exchange.items() if k not in _EXCLUDED_FIELDS}
    return json.dumps(
        fields,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def sign_exchange(exchange: dict[str, Any], secret: str) -> str:
    """Return the hex HMAC-SHA256 signature of *exchange* using *secret*.

    The signature is deterministic for a given Exchange content and secret: the
    same Exchange signs to the same value every time (Task 5.2 can recompute it
    to verify), and any change to a signed field changes the signature.
    """
    message = canonical_payload(exchange).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_exchange(exchange: dict[str, Any], secret: str) -> bool:
    """Return ``True`` when *exchange* carries a signature matching *secret*.

    Reads the ``signature`` field, recomputes the HMAC over the canonical
    content, and compares in constant time. A missing or malformed signature
    returns ``False``. Used by the retry-save endpoint (Task 5.2) to refuse
    forged or edited Exchanges.
    """
    provided = exchange.get("signature")
    if not isinstance(provided, str) or not provided:
        return False
    expected = sign_exchange(exchange, secret)
    return hmac.compare_digest(provided, expected)
