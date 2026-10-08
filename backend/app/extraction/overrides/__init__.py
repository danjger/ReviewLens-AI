"""Optional host-specific extraction overrides (Requirement 7).

The Extraction Engine is AI-first and site-agnostic: nothing in the product
needs an override to work.  This package is a *decoupled extension point* for
the rare case where hand-written, host-specific extraction code would do a
better job than the AI Locator on some site.  **The registry ships empty**
(Requirement 7.1): no built-in overrides are registered, and no feature may
depend on one existing.

Design: see ``.kiro/specs/review-extraction/design.md`` ("Host overrides
(overrides/)"):

    A registry keyed by host.  Each override returns a ``LocatorResult``-shaped
    object, which goes through the same post-processing and validation.  The
    registry ships empty.

An override is just *code* — it never calls the AI.  But its output is held to
exactly the same standard as a Review Locator result (Requirement 7.2: "use its
output only if it passes the same validation as a Review Locator result").  So
an override returns a :class:`~app.extraction.models.LocatorResult` (the same
shape the Locator produces, pointing at elements by reference ID), and
:func:`run_override` routes that result through:

1. :func:`app.extraction.postprocess.postprocess` — the *same* post-processing
   that reads review text from the referenced elements with code (the AI, and
   likewise an override, may never supply review text); and
2. :func:`app.extraction.selectors.validate_selectors` — the *same* selector
   validation ``build_plan`` applies to a Locator result when choosing the
   ``selectors`` method (Requirement 4.1).

Operational definition of "passes the same validation as a Review Locator
result" (:data:`run_override`): the override's post-processed output must

- yield **at least one Verified Review** (post-processing read real review text
  from real elements), **and**
- have its suggested selectors **validate** against those Verified Reviews
  (``validate_selectors(...).is_valid`` with the configured
  ``selector_min_agreement``), exactly the gate ``build_plan`` uses to trust a
  Locator's selectors.

This mirrors ``build_plan``/``extract``: there, a Locator result is only trusted
to the extent it post-processes into Verified Reviews and its selectors pass
``validate_selectors``.  An override is held to the *same* bar — if it passes it
is used, and if it fails it is rejected and the caller falls back to the normal
AI path (no feature requires the override, Requirement 7.1).

Host key: overrides are keyed by **registrable domain** (eTLD+1), computed with
``tldextract`` using the bundled suffix snapshot (no network) — the same
host notion ``pagination`` uses for same-site filtering, so a registration for
``example.com`` applies to ``www.example.com``, ``shop.example.com``, and so on.
``register`` accepts a bare host or domain and normalizes it; ``get_override``
accepts a bare host **or** a full URL.

Thread-safety / purity: registration mutates a module-level dict and is expected
to happen at import time (if ever); lookup and :func:`run_override` are pure and
deterministic.  The registry being empty by default keeps the engine's behaviour
unchanged unless a developer deliberately registers an override.
"""

from __future__ import annotations

from typing import Protocol

import tldextract

from app.core.config import get_settings
from app.extraction import postprocess, selectors
from app.extraction.models import CleanedPage, LocatorResult, VerifiedReview

__all__ = [
    "HostOverride",
    "OverrideResult",
    "register",
    "unregister",
    "get_override",
    "registered_hosts",
    "clear_registry",
    "run_override",
    "host_key",
]


# ---------------------------------------------------------------------------
# Override interface
# ---------------------------------------------------------------------------


class HostOverride(Protocol):
    """A host-specific extraction override.

    An override is pure code (no AI) that inspects a page and points at its
    reviews the same way the Review Locator does: by returning a
    :class:`~app.extraction.models.LocatorResult` whose items reference elements
    by their cleaner reference ID, and whose ``selectors`` suggest how to read
    similar pages.  Review *text* is never supplied by the override — it is read
    from the referenced elements by :func:`run_override` via post-processing,
    exactly as for a Locator result.

    An override returns ``None`` to decline (for example, the page is not the
    layout it handles), in which case the caller falls back to the normal AI
    path.
    """

    def __call__(
        self,
        html: str,
        url: str,
        cleaned: CleanedPage,
    ) -> LocatorResult | None:
        """Produce a Locator-shaped result for ``html``, or ``None`` to decline.

        :param html: The original rendered HTML (review text is read from here
            by code during post-processing).
        :param url: The page's final URL.
        :param cleaned: The Cleaned Page for ``html`` (its ``lookup`` resolves
            the reference IDs the override points at).
        :returns: A :class:`LocatorResult`, or ``None`` to decline.
        """
        ...


class OverrideResult:
    """The verified outcome of running an override through the shared gate.

    ``passed`` is the final verdict — ``True`` only when the override's output
    cleared the *same* validation a Locator result must clear (at least one
    Verified Review **and** valid selectors).  ``reviews`` are the Verified
    Reviews read from the page by code (empty when ``passed`` is ``False``);
    ``discarded`` are the post-processing discard counts; ``result`` is the raw
    :class:`LocatorResult` the override returned (``None`` when the override
    declined).

    Callers use ``passed``/``reviews`` to decide whether to use the override's
    output; a non-passing result means "fall back to the AI path".
    """

    __slots__ = ("passed", "reviews", "discarded", "result")

    def __init__(
        self,
        *,
        passed: bool,
        reviews: list[VerifiedReview],
        discarded: dict[str, int],
        result: LocatorResult | None,
    ) -> None:
        self.passed = passed
        self.reviews = reviews
        self.discarded = discarded
        self.result = result

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"OverrideResult(passed={self.passed}, "
            f"reviews={len(self.reviews)}, discarded={self.discarded})"
        )


#: A result for "no override exists / the override declined": nothing passed,
#: no reviews, no raw result.  Returned by :func:`run_override` in those cases.
_DECLINED = OverrideResult(passed=False, reviews=[], discarded={}, result=None)


# ---------------------------------------------------------------------------
# Host key normalization (registrable domain, bundled suffix snapshot)
# ---------------------------------------------------------------------------

#: ``tldextract`` configured with no suffix-list URLs, so it uses only the
#: bundled snapshot and never touches the network — the same configuration
#: ``pagination`` uses, so override host keys match the same-site notion used
#: elsewhere in the engine.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())


def host_key(host_or_url: str) -> str:
    """Normalize a host or URL to its registrable domain (eTLD+1).

    Accepts either a bare host (``"www.example.com"``), a registrable domain
    (``"example.com"``), or a full URL (``"https://shop.example.com/reviews"``)
    and returns the registrable domain used as the registry key.  Falls back to
    the lower-cased, stripped input when there is no registrable domain (an IP,
    ``localhost``, or an unparseable value) so such keys are still stable and
    round-trip through :func:`register` / :func:`get_override`.

    :param host_or_url: A host, domain, or URL.
    :returns: The registrable domain, or a normalized fallback key.
    """
    extracted = _EXTRACT(host_or_url)
    domain = getattr(extracted, "top_domain_under_public_suffix", None)
    if not domain:
        domain = getattr(extracted, "registered_domain", "")
    if domain:
        return str(domain).lower()
    return host_or_url.strip().lower()


# ---------------------------------------------------------------------------
# The registry — ships EMPTY (Requirement 7.1)
# ---------------------------------------------------------------------------

#: Module-level registry mapping a registrable-domain host key to its override.
#: **Empty by default** — no built-in overrides.  Mutated only through
#: :func:`register` / :func:`unregister` / :func:`clear_registry`.
_REGISTRY: dict[str, HostOverride] = {}


def register(host: str, override: HostOverride) -> None:
    """Register ``override`` for ``host`` (keyed by registrable domain).

    ``host`` may be a bare host, a registrable domain, or a URL; it is
    normalized with :func:`host_key`.  Registering a host that already has an
    override replaces it (last registration wins).

    No feature registers an override — this exists so one *can* be added later
    without the engine depending on it (Requirement 7.1).

    :param host: The host/domain/URL the override applies to.
    :param override: The :class:`HostOverride` callable.
    """
    _REGISTRY[host_key(host)] = override


def unregister(host: str) -> None:
    """Remove any override registered for ``host`` (no-op if none).

    ``host`` is normalized with :func:`host_key`.  Useful for tests and for
    deliberately disabling an override at runtime.
    """
    _REGISTRY.pop(host_key(host), None)


def clear_registry() -> None:
    """Remove every registered override, returning the registry to empty.

    Primarily for tests, so a registration in one test cannot leak into another
    and the shipped-empty invariant can be restored.
    """
    _REGISTRY.clear()


def registered_hosts() -> frozenset[str]:
    """Return the set of registered host keys (empty by default).

    A snapshot, so callers cannot mutate the registry through it.
    """
    return frozenset(_REGISTRY)


def get_override(host_or_url: str) -> HostOverride | None:
    """Return the override registered for a host/URL, or ``None``.

    ``host_or_url`` may be a bare host, a registrable domain, or a full URL; it
    is normalized to its registrable domain with :func:`host_key` before lookup.
    Returns ``None`` for any host when the registry is empty (the default), so
    callers always have a well-defined "no override" path (Requirement 7.1).

    :param host_or_url: A host, domain, or URL to look up.
    :returns: The registered :class:`HostOverride`, or ``None``.
    """
    return _REGISTRY.get(host_key(host_or_url))


# ---------------------------------------------------------------------------
# Running an override through the SAME post-processing + validation
# ---------------------------------------------------------------------------


def run_override(
    html: str,
    url: str,
    cleaned: CleanedPage,
    *,
    override: HostOverride | None = None,
    min_agreement: float | None = None,
) -> OverrideResult:
    """Run the override for ``url`` and gate its output like a Locator result.

    Resolves the override (the one passed in, else :func:`get_override` for
    ``url``) and, when one exists and does not decline, routes its
    :class:`LocatorResult` through the **same** two steps a Locator result goes
    through in ``build_plan``:

    1. :func:`app.extraction.postprocess.postprocess` reads each review's text
       (and author/date/title/rating) from the referenced elements **with
       code**, discarding by the same rules (unknown ref, too short, duplicate,
       non-review kind).  An override can no more invent review text than the AI
       can — the text comes from the page.
    2. :func:`app.extraction.selectors.validate_selectors` checks the override's
       suggested selectors reproduce those Verified Reviews, using the configured
       ``selector_min_agreement`` (the exact gate ``build_plan`` uses to trust a
       Locator's selectors).

    "Passes the same validation as a Review Locator result" is therefore
    operationally: **at least one Verified Review was produced AND the selectors
    validate**.  On pass, the :class:`OverrideResult` carries ``passed=True`` and
    the Verified Reviews; on fail (or when there is no override, or the override
    declined), ``passed=False`` with no reviews, and the caller falls back to the
    normal AI path (Requirement 7.1, 7.2).

    :param html: The original rendered HTML (review text is read from it).
    :param url: The page's final URL (used to look up an override when one is
        not passed explicitly).
    :param cleaned: The Cleaned Page for ``html`` (its ``lookup`` resolves refs).
    :param override: An explicit override to run; when ``None``, the registry is
        consulted via :func:`get_override`.
    :param min_agreement: Selector agreement threshold; defaults to the
        configured ``selector_min_agreement`` so the gate matches ``build_plan``.
    :returns: The :class:`OverrideResult` with the verdict and verified reviews.
    """
    chosen = override if override is not None else get_override(url)
    if chosen is None:
        return _DECLINED

    result = chosen(html, url, cleaned)
    if result is None:
        return _DECLINED

    # SAME post-processing as the Locator path: read text from refs with code.
    reviews, discarded = postprocess.postprocess(html, cleaned.lookup, result)

    # No Verified Reviews → the override produced nothing trustworthy; reject
    # (an empty-verified result could never be a validated Locator result either).
    if not reviews:
        return OverrideResult(passed=False, reviews=[], discarded=discarded, result=result)

    # SAME selector validation as build_plan applies to a Locator result.
    threshold = (
        min_agreement if min_agreement is not None else get_settings().selector_min_agreement
    )
    validation = selectors.validate_selectors(
        html,
        result.selectors,
        reviews,
        min_agreement=threshold,
    )

    return OverrideResult(
        passed=validation.is_valid,
        reviews=reviews if validation.is_valid else [],
        discarded=discarded,
        result=result,
    )
