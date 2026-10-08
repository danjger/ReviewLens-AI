"""Unit tests for app.ingestion.prescan.prescan.

Covers the rule-based viability pre-scan (dataset-ingestion Requirement 3.2 /
3.5): empty shells, bot/CAPTCHA challenge pages, and login/paywall walls are
each detected without an AI call, while a content-rich review page passes
through (returns ``None``). The pre-scan is free and deterministic, so these
tests use plain HTML strings and the default configuration.
"""

from __future__ import annotations

import pytest
from app.core.config import get_settings
from app.ingestion.prescan import Blocker, prescan


@pytest.fixture(autouse=True)
def _fresh_settings() -> None:
    """Reset the memoised settings so the default threshold is used."""
    get_settings.cache_clear()


def _page(body: str, *, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def _long_review_body(char_target: int = 1200) -> str:
    """Return a content-rich review page body well over every threshold."""
    sentence = (
        "<article><h2>Great product</h2>"
        "<p>Setup took an afternoon and the whole team was productive by the "
        "end of the week. Support answered within the hour.</p></article>"
    )
    reviews = sentence * ((char_target // len(sentence)) + 2)
    return f"<main><h1>Acme CRM Reviews</h1>{reviews}</main>"


# ---------------------------------------------------------------------------
# No blocker (content pages pass)
# ---------------------------------------------------------------------------


class TestNoBlocker:
    def test_content_rich_page_passes(self) -> None:
        assert prescan(_page(_long_review_body())) is None

    def test_incidental_login_box_on_content_page_passes(self) -> None:
        # A small header login form on an otherwise content-rich page is not a
        # wall: the password field alone must not block it.
        body = (
            '<header><form><input type="password" name="pw"></form></header>' + _long_review_body()
        )
        assert prescan(_page(body)) is None

    def test_word_captcha_in_prose_does_not_block_content_page(self) -> None:
        # The word appears in a sentence, not as a challenge fingerprint/phrase.
        body = _long_review_body() + "<p>We later added a captcha to our form.</p>"
        assert prescan(_page(body)) is None


# ---------------------------------------------------------------------------
# Empty shell
# ---------------------------------------------------------------------------


class TestEmptyShell:
    def test_empty_body_is_empty_shell(self) -> None:
        assert prescan(_page("")) is Blocker.EMPTY_SHELL

    def test_js_shell_with_only_scripts_is_empty_shell(self) -> None:
        # A hydration root plus a big script bundle: no visible text.
        body = '<div id="root"></div><script>' + ("x=1;" * 500) + "</script>"
        assert prescan(_page(body)) is Blocker.EMPTY_SHELL

    def test_script_text_does_not_count_toward_visible_text(self) -> None:
        # Lots of script/style content but almost no visible text → shell.
        head = "<style>" + (".a{color:red}" * 200) + "</style>"
        body = "<p>Loading…</p><script>" + ("var a=1;" * 200) + "</script>"
        assert prescan(_page(body, head=head)) is Blocker.EMPTY_SHELL

    def test_just_below_threshold_is_empty_shell(self) -> None:
        threshold = get_settings().viability_empty_shell_min_chars
        body = "<p>" + ("a" * (threshold - 1)) + "</p>"
        assert prescan(_page(body)) is Blocker.EMPTY_SHELL

    def test_at_threshold_passes(self) -> None:
        threshold = get_settings().viability_empty_shell_min_chars
        body = "<p>" + ("a" * threshold) + "</p>"
        assert prescan(_page(body)) is None

    def test_missing_body_is_empty_shell(self) -> None:
        assert prescan("<html><head><title>x</title></head></html>") is Blocker.EMPTY_SHELL


# ---------------------------------------------------------------------------
# Challenge pages
# ---------------------------------------------------------------------------


class TestChallenge:
    def test_recaptcha_widget_fingerprint(self) -> None:
        body = _long_review_body() + '<div class="g-recaptcha" data-sitekey="k"></div>'
        assert prescan(_page(body)) is Blocker.CHALLENGE

    def test_cloudflare_turnstile_fingerprint(self) -> None:
        head = '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script>'
        body = "<h1>Just a moment…</h1>" + _long_review_body()
        assert prescan(_page(body, head=head)) is Blocker.CHALLENGE

    def test_verify_you_are_human_phrase(self) -> None:
        body = (
            "<main><h1>Security check</h1>"
            "<p>Please verify you are a human to continue.</p>"
            + ("<p>Additional notice text here.</p>" * 20)
            + "</main>"
        )
        assert prescan(_page(body)) is Blocker.CHALLENGE

    def test_checking_your_browser_phrase(self) -> None:
        body = (
            "<main><h1>Please wait</h1>"
            "<p>Checking your browser before accessing the site.</p>"
            + ("<p>This process is automatic.</p>" * 20)
            + "</main>"
        )
        assert prescan(_page(body)) is Blocker.CHALLENGE


# ---------------------------------------------------------------------------
# Login / paywall walls
# ---------------------------------------------------------------------------


class TestLoginWall:
    def test_password_form_dominated_page(self) -> None:
        body = (
            '<main><h1>Sign in</h1><form><input type="email"><input type="password"></form></main>'
        )
        assert prescan(_page(body)) is Blocker.LOGIN_WALL

    def test_paywall_phrase_with_password_form(self) -> None:
        # Content-rich page but gated: explicit paywall phrase + password form.
        body = (
            _long_review_body()
            + "<p>Subscribe to continue reading the full reviews.</p>"
            + '<form><input type="password" name="pw"></form>'
        )
        assert prescan(_page(body)) is Blocker.LOGIN_WALL

    def test_login_wall_takes_priority_over_content_length(self) -> None:
        # Short page, password form, no paywall phrase → wall by dominance.
        body = '<h1>Members only</h1><form><input type="password" name="pw"></form>'
        assert prescan(_page(body)) is Blocker.LOGIN_WALL


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_challenge_fingerprint_wins_over_empty_shell(self) -> None:
        # A tiny challenge page (little visible text, but a known widget
        # fingerprint) is reported as the more specific CHALLENGE rather than
        # the generic empty-shell fallback. Either way it yields wont_work.
        body = '<div class="g-recaptcha"></div>'
        assert prescan(_page(body)) is Blocker.CHALLENGE

    def test_login_form_wins_over_empty_shell(self) -> None:
        # A bare password form is a login wall, not merely an empty shell.
        body = '<form><input type="password" name="pw"></form>'
        assert prescan(_page(body)) is Blocker.LOGIN_WALL
