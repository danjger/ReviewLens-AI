"""Rule-based viability pre-scan (free, no AI call).

Step 1 of ``viability.assess()`` (see the dataset-ingestion design, "Viability
assessment (AI-first)"). Before any AI is spent, the rendered HTML is checked
with cheap, deterministic rules for three *certain* blockers:

- **empty_shell** — fewer than ``VIABILITY_EMPTY_SHELL_MIN_CHARS`` characters of
  visible text (an un-hydrated JavaScript shell with nothing readable).
- **challenge** — a bot/CAPTCHA challenge or "verify you are human" interstitial,
  recognised from well-known widget fingerprints and challenge wording.
- **login_wall** — a login or paywall page dominated by a password form.

A certain blocker lets the check stop here with a ``wont_work`` verdict and no
AI call (Requirement 3.2, 3.5). When no rule fires the function returns
``None`` and the caller proceeds to structured data and the Review Locator.

The scan is rule-based and free: it only parses the HTML with ``selectolax``
and reads a configured threshold. It makes no network or AI calls, and keeps no
state, so it is safe to run in either compute mode.
"""

from __future__ import annotations

import re
from enum import StrEnum

from selectolax.parser import HTMLParser, Node

from app.core.config import get_settings

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class Blocker(StrEnum):
    """A certain blocker found by the rule-based pre-scan.

    The string values are stable identifiers suitable for storing in the
    verdict's ``evidence.blocker`` field and for log fields.
    """

    EMPTY_SHELL = "empty_shell"
    CHALLENGE = "challenge"
    LOGIN_WALL = "login_wall"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

#: Subtrees whose text is never "visible" page content. Mirrors the Extraction
#: Engine cleaner's dropped tags so the empty-shell measure matches what a
#: reader would see (a ``<script>`` body full of JSON must not count as text).
_NON_VISIBLE_TAGS: tuple[str, ...] = ("script", "style", "noscript", "template")

#: Collapse any run of whitespace (including newlines) to a single space.
_WS_RE = re.compile(r"\s+")


def _visible_text(tree: HTMLParser) -> str:
    """Return the page's visible text, whitespace-normalized.

    Reads the body text with code (selectolax), after removing non-visible
    subtrees so script/style content never inflates the character count.
    Returns an empty string for a page with no ``<body>``.
    """
    body = tree.body
    if body is None:
        return ""
    for tag in _NON_VISIBLE_TAGS:
        for node in body.css(tag):
            node.decompose()
    text = body.text(separator=" ")
    return _WS_RE.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# Challenge detection
# ---------------------------------------------------------------------------

#: Known bot-protection / CAPTCHA widget fingerprints. Any of these substrings
#: appearing in the raw HTML (class names, script src, element ids) is a strong
#: signal of a challenge interstitial rather than a content page.
_CHALLENGE_FINGERPRINTS: tuple[str, ...] = (
    "g-recaptcha",
    "h-captcha",
    "hcaptcha",
    "recaptcha",
    "cf-challenge",
    "cf-turnstile",
    "challenge-platform",  # Cloudflare challenge bundle path
    "px-captcha",  # PerimeterX
    "_px",  # PerimeterX cookie/script hooks
    "distil",  # Distil Networks
    "incapsula",  # Imperva Incapsula
    "datadome",  # DataDome
    "arkoselabs",
    "funcaptcha",
)

#: Challenge wording shown to a human on an interstitial. Matched against the
#: visible text (lower-cased) so ordinary content mentioning "captcha" in a
#: sentence is less likely to trip it than a dedicated challenge page would.
_CHALLENGE_PHRASES: tuple[str, ...] = (
    "verify you are human",
    "verify you are a human",
    "are you a human",
    "i'm not a robot",
    "im not a robot",
    "checking your browser",
    "please verify you are a human",
    "complete the security check",
    "enable javascript and cookies to continue",
    "unusual traffic from your",
    "prove you are not a robot",
)


def _is_challenge(html: str, visible_text: str) -> bool:
    """Return True when the page looks like a bot/CAPTCHA challenge."""
    html_lower = html.lower()
    if any(fp in html_lower for fp in _CHALLENGE_FINGERPRINTS):
        return True
    text_lower = visible_text.lower()
    return any(phrase in text_lower for phrase in _CHALLENGE_PHRASES)


# ---------------------------------------------------------------------------
# Login / paywall detection
# ---------------------------------------------------------------------------

#: Visible-text wording typical of a paywall or members-only interstitial. Used
#: together with a dominant password form; a password field alone (e.g. a small
#: header login box) does not block a content page.
_LOGIN_PHRASES: tuple[str, ...] = (
    "subscribe to continue",
    "subscribe to read",
    "sign in to continue",
    "log in to continue",
    "please log in to continue",
    "members only",
    "this content is for subscribers",
    "to continue reading",
)


def _password_inputs(tree: HTMLParser) -> list[Node]:
    """Return every ``<input type=password>`` node in the document."""
    return [
        node
        for node in tree.css("input")
        if (node.attributes.get("type") or "").strip().lower() == "password"
    ]


def _is_login_wall(tree: HTMLParser, visible_text: str) -> bool:
    """Return True when the page is dominated by a login / paywall form.

    "Dominated" means a password form is present *and* the page has little
    other readable content (short visible text), or the visible text carries
    an explicit paywall/login-wall phrase. A password field embedded in an
    otherwise content-rich page (a corner login box) is not a blocker.
    """
    if not _password_inputs(tree):
        return False

    text_lower = visible_text.lower()
    if any(phrase in text_lower for phrase in _LOGIN_PHRASES):
        return True

    # A password form on a page with very little other content is a wall:
    # the form is the page. The threshold is generous (well above the
    # empty-shell cutoff) because real content pages carry far more text than
    # a login form's labels.
    return len(visible_text) < _LOGIN_WALL_MAX_CHARS


#: A password-bearing page with fewer visible-text characters than this is
#: treated as a login/paywall wall (the form dominates). Deliberately larger
#: than the empty-shell threshold so a genuine content page with an incidental
#: login box is not misclassified.
_LOGIN_WALL_MAX_CHARS = 1000


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def prescan(html: str) -> Blocker | None:
    """Run the rule-based pre-scan over rendered *html*.

    Order matters: an empty shell is detected first (there is nothing to read
    at all), then challenge pages, then login/paywall walls.

    Args:
        html: The rendered HTML saved by ``capture.render()``.

    Returns:
        A :class:`Blocker` when a certain blocker is found (the caller stops
        with ``wont_work`` and spends no AI), or ``None`` when the page passes
        the pre-scan and should proceed to structured data and the Locator.
    """
    settings = get_settings()
    tree = HTMLParser(html or "")
    visible_text = _visible_text(tree)

    # Positive signals first: a challenge widget or a dominant password form is
    # a definite classification regardless of how much text the page carries,
    # and is more informative than the generic empty-shell fallback.
    if _is_challenge(html, visible_text):
        return Blocker.CHALLENGE

    if _is_login_wall(tree, visible_text):
        return Blocker.LOGIN_WALL

    # Otherwise, a page with almost no readable content is an un-hydrated shell.
    if len(visible_text) < settings.viability_empty_shell_min_chars:
        return Blocker.EMPTY_SHELL

    return None
