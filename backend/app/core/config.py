"""Application configuration.

Settings are read from environment variables (via pydantic-settings). When a
``SECRETS_ARN`` environment variable is present the named Secrets Manager
secret is fetched at startup and its JSON key/value pairs are merged into the
environment before the Settings object is constructed, so secrets never have
to be written to .env files.

Usage::

    from app.core.config import get_settings

    settings = get_settings()

``get_settings()`` is memoised with ``functools.cache`` so the secret fetch
happens at most once per process. In tests, monkeypatch the environment
variables you need and call ``get_settings.cache_clear()`` to reset.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from typing import Annotated, Any

import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Secrets Manager bootstrap
# ---------------------------------------------------------------------------


def _load_secrets_into_env(secrets_arn: str) -> None:
    """Fetch a Secrets Manager secret and inject its keys into ``os.environ``.

    Only keys that are *not* already present in the environment are set, so
    explicit environment variables always win over secrets (same precedence
    rule as a .env file).

    Raises ``RuntimeError`` when the secret cannot be fetched so that the
    process refuses to start rather than running with missing credentials.
    """
    try:
        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secrets_arn)
        secret_string = response.get("SecretString")
        if not secret_string:
            raise RuntimeError(f"Secrets Manager secret {secrets_arn!r} has no SecretString")
        values: dict[str, Any] = json.loads(secret_string)
    except ClientError as exc:
        raise RuntimeError(f"Failed to fetch secret {secrets_arn!r}: {exc}") from exc

    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = str(value)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _strip_inline_comment(value: object) -> object:
    """Strip a trailing ``\u0020#`` inline comment and surrounding whitespace.

    ``.env`` lines in this repo carry inline comments after the value, e.g.
    ``AWS_ENDPOINT_URL=http://localhost:4566 # LocalStack endpoint``. The
    dotenv/env sources hand the whole right-hand side through as the value, so
    without stripping, the comment ends up inside the value (here breaking URL
    port parsing). Following the common dotenv convention, a comment starts at
    the first ``#`` that is preceded by whitespace; a ``#`` with no preceding
    space (e.g. a URL fragment or a value that is wholly a ``#...`` token) is
    left intact. Non-string values pass through unchanged.
    """
    if not isinstance(value, str):
        return value
    out = value
    for i in range(1, len(out)):
        if out[i] == "#" and out[i - 1] in " \t":
            out = out[:i]
            break
    return out.strip()


class _CommentStrippingSource(PydanticBaseSettingsSource):
    """Wrap an env/dotenv source, stripping inline comments from its values.

    pydantic-settings reads raw strings from the environment and ``.env`` before
    any field validator runs; this wrapper post-processes those raw strings so a
    trailing inline comment never reaches field parsing (see
    :func:`_strip_inline_comment`). It delegates everything to the wrapped source
    and only rewrites the returned values.
    """

    def __init__(self, wrapped: PydanticBaseSettingsSource) -> None:
        self._wrapped = wrapped
        super().__init__(wrapped.settings_cls)

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:  # pragma: no cover
        # Not used directly: __call__ delegates to the wrapped source. Present to
        # satisfy the abstract base; delegate for completeness.
        return self._wrapped.get_field_value(field, field_name)

    def __call__(self) -> dict[str, object]:
        return {k: _strip_inline_comment(v) for k, v in self._wrapped().items()}


class Settings(BaseSettings):
    """All tunable limits and runtime configuration for ReviewLens AI.

    Values are read from environment variables (case-insensitive).  When
    ``SECRETS_ARN`` is set, secrets are loaded into the environment before
    pydantic-settings reads them, so every field is resolved from env vars
    regardless of where they originated.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Don't fail on extra env vars – the environment often has unrelated
        # variables from Docker Compose, CI, or the developer's shell.
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Strip inline comments from env/.env values before fields parse them.

        Wraps the environment and dotenv sources with
        :class:`_CommentStrippingSource` so a trailing inline comment in ``.env``
        (``KEY=value # note``) is removed from the value rather than parsed as
        part of it. Source precedence is unchanged (init > env > .env > secrets).
        """
        return (
            init_settings,
            _CommentStrippingSource(env_settings),
            _CommentStrippingSource(dotenv_settings),
            file_secret_settings,
        )

    # ------------------------------------------------------------------
    # Environment / deployment identity
    # ------------------------------------------------------------------

    env: str = "development"
    """Deployment environment: ``development``, ``test``, or ``production``."""

    service_name: str = "api"
    """Name of this service instance, injected as a log field."""

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------

    origin_verify_secret: str = ""
    """Value of the ``X-Origin-Verify`` header that CloudFront adds.
    Empty string disables the check in local development."""

    ssrf_test_allow_hosts: str = ""
    """Comma-separated hostnames allowed for SSRF tests (e.g. ``fixtures``).
    MUST be empty in production – the process refuses to start otherwise."""

    chat_signing_secret: str = ""
    """Server-side secret used to HMAC-sign saved chat Exchange payloads.

    The chat service signs each Exchange's ``done`` payload with this secret
    (guardrailed-chat Task 4.3); the retry-save endpoint (Task 5.2) verifies the
    signature so visitors can't write forged or edited Exchanges into the shared
    history (design "Endpoints"; Correctness Property 4). Sourced from Secrets
    Manager (or env) like every other secret, never a literal. Empty in local
    development, where the signature is still computed and verifiable but is not
    a security boundary (there is no untrusted network between the browser and
    the service)."""

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    s3_bucket: str = ""
    """Name of the application S3 bucket."""

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    database_url: str = ""
    """psycopg connection URL used in local/CI mode.
    Unused in AWS where the Data API connection is configured separately."""

    # Aurora Data API (AWS only)
    db_resource_arn: str = ""
    db_secret_arn: str = ""
    db_database_name: str = "reviewlens"

    # ------------------------------------------------------------------
    # Queue URLs
    # ------------------------------------------------------------------

    check_queue_url: str = ""
    processing_queue_url: str = ""
    push_queue_url: str = ""

    # ------------------------------------------------------------------
    # EventBridge
    # ------------------------------------------------------------------

    eventbridge_bus_name: str = "default"
    """Name of the EventBridge event bus that domain events are published to.
    Defaults to ``default`` so local/LocalStack runs work without extra setup;
    AWS deployments set this to the application bus."""

    # ------------------------------------------------------------------
    # Real-time push (WebSocket API)
    # ------------------------------------------------------------------

    ws_api_endpoint: str = ""
    """Callback endpoint for the API Gateway WebSocket API, used by the push
    consumer to ``postToConnection``. In AWS this is the stage's HTTPS callback
    URL (``https://<id>.execute-api.<region>.amazonaws.com/<stage>``) injected
    by the RealtimeStack; locally it points at LocalStack. Empty means no push
    channel is configured (the handler then has nothing to deliver to)."""

    ws_connections_table: str = "ws-connections"
    """DynamoDB table name holding active WebSocket connection IDs. The table
    (PK ``connection_id``, TTL ``ttl``) is provisioned by ``platform-foundation``;
    this module only reads the name."""

    # ------------------------------------------------------------------
    # AI models
    # ------------------------------------------------------------------

    claude_chat_model: str = "claude-sonnet-5-5"
    """Model ID used for interactive Q&A (guardrailed chat)."""

    claude_extract_model: str = "claude-haiku-4-5-20251001"
    """Model ID used for page reading and review extraction."""

    claude_precheck_model: str = "claude-haiku-4-5-20251001"
    """Model ID used for URL viability pre-checks."""

    # ------------------------------------------------------------------
    # Crawl and data limits
    # ------------------------------------------------------------------

    max_pages: int = 10
    """Maximum number of pages to crawl per dataset."""

    max_reviews: int = 1000
    """Maximum number of reviews to store per dataset."""

    max_urls_per_check: int = 10
    """Maximum number of URLs allowed in a single check request."""

    page_request_delay_s: float = 1.0
    """Minimum delay, in seconds, between page requests to the same host during
    collection (review-analysis Requirement 2.4). A polite crawl delay so the
    Worker does not hammer a source while paginating."""

    collection_time_budget_s: int = 12 * 60
    """Soft wall-clock budget for the page-collection stage, in seconds. When
    collection approaches this (design: "If collecting pages approaches 12
    minutes, stop collecting …"), it stops and continues with the pages already
    gathered, leaving the rest of the 15-minute Lambda budget for extraction,
    metrics, and completion (review-analysis Requirement 2, design Error
    Handling)."""

    check_timeout_s: int = 60
    """Per-URL check timeout in seconds."""

    max_upload_mb: int = 10
    """Maximum CSV upload file size in megabytes."""

    # ------------------------------------------------------------------
    # Viability
    # ------------------------------------------------------------------

    viability_min_reviews: int = 5
    """Minimum number of reviews required for a page to be considered viable."""

    viability_empty_shell_min_chars: int = 300
    """Minimum number of visible-text characters a rendered page must have to
    avoid an ``empty_shell`` pre-scan blocker. A page with fewer visible-text
    characters is treated as an empty JavaScript shell and gets a ``wont_work``
    verdict without an AI call (Requirement 3.2 / 3.5)."""

    viability_reported_total_multiplier: float = 2.0
    """How many times the verified count the page may *report* as its total
    before the page is treated as showing "many more than shown". A page that
    reports no more than ``verified × this multiplier`` reviews is not held back
    for an unseen remainder (Requirement 3.4); above it, with no next page, the
    verdict is capped at ``limited`` (Requirement 3.6)."""

    viability_max_rejection_fraction: float = 0.20
    """Maximum fraction of the Locator's reviews that may fail verification
    before the verdict is capped at ``limited``. When
    ``rejected / (verified + rejected)`` exceeds this fraction the reading is
    judged unreliable (Requirement 3.6)."""

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    extraction_strategy: str = "auto"
    """Extraction strategy: ``auto``, ``structured``, or ``ai``."""

    extract_page_token_budget: int = 30_000
    """Maximum tokens to send to the AI when extracting a single page."""

    selector_min_agreement: float = 0.8
    """Minimum fraction of sampled reviews that must agree for a CSS selector
    to be accepted as valid."""

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    chat_corpus_token_budget: int = 150_000
    """Maximum tokens used for the review corpus in a single chat request."""

    # ------------------------------------------------------------------
    # URL normalisation
    # ------------------------------------------------------------------

    tracking_params: Annotated[list[str], NoDecode] = [
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "msclkid",
        "ref",
        "source",
    ]
    """Query-string parameters stripped during URL normalisation."""

    # ------------------------------------------------------------------
    # Rate limits
    # ------------------------------------------------------------------

    rl_checks_per_ip_hour: int = 30
    """Maximum URL check requests per client IP per hour."""

    rl_uploads_per_ip_hour: int = 30
    """Maximum upload (pre-signed URL) requests per client IP per hour.

    Guards ``POST /uploads`` (dataset-ingestion task 8.1) with the same per-IP
    and global ``uploads`` counters the Check endpoints use for ``checks``."""

    rl_questions_per_ip_hour: int = 120
    """Maximum chat questions per client IP per hour."""

    rl_global_ai_calls_per_hour: int = 2000
    """Maximum AI API calls across all clients per hour."""

    dynamodb_rate_limit_table: str = "rate-limits"
    """DynamoDB table name for rate-limit counters."""

    check_sessions_table: str = "check-sessions"
    """DynamoDB table name for Check Sessions (TTL 24 h).

    Each item holds one Check run: its origin, the optional dataset being
    refreshed, and a map of per-URL items keyed by ``item_id`` so each item can
    be updated independently with a conditional expression. The table itself is
    provisioned by ``platform-foundation``'s infrastructure; this module only
    reads the name."""

    # ------------------------------------------------------------------
    # Sweeper
    # ------------------------------------------------------------------

    sweep_requested_after_min: int = 5
    """Re-enqueue datasets that have been in ``requested`` longer than this
    many minutes without being picked up."""

    sweep_processing_stale_min: int = 20
    """Mark datasets ``failed`` that have been stuck in ``processing`` longer
    than this many minutes."""

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    aws_endpoint_url: str = ""
    """Override for AWS service endpoints (used by LocalStack in tests)."""

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("tracking_params", mode="before")
    @classmethod
    def _parse_tracking_params(cls, v: object) -> object:
        """Accept a comma-separated string or a JSON array for the env var.

        ``.env`` carries ``TRACKING_PARAMS`` as a comma-separated list
        (``utm_source,utm_medium,...``). ``NoDecode`` on the field stops
        pydantic-settings from JSON-decoding it, so this parses the raw
        string here: a JSON array is honoured if given, otherwise the value
        is split on commas with surrounding whitespace trimmed and blanks
        dropped. A real list (the Python default) passes through unchanged.
        """
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith("["):
                import json

                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return v

    @field_validator("selector_min_agreement")
    @classmethod
    def _selector_agreement_range(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError(
                f"selector_min_agreement must be between 0 (exclusive) and 1 (inclusive), got {v}"
            )
        return v

    @field_validator("viability_max_rejection_fraction")
    @classmethod
    def _rejection_fraction_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(
                f"viability_max_rejection_fraction must be between 0 and 1 (inclusive), got {v}"
            )
        return v

    @field_validator("viability_reported_total_multiplier")
    @classmethod
    def _reported_multiplier_range(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError(f"viability_reported_total_multiplier must be at least 1, got {v}")
        return v

    @model_validator(mode="after")
    def _forbid_ssrf_allowlist_in_production(self) -> Settings:
        """Refuse to start in production when a test SSRF allowlist is set.

        This prevents misconfigured production deployments from silently
        allowing requests to private network hosts that are only safe in tests.
        """
        if self.env == "production" and self.ssrf_test_allow_hosts:
            raise ValueError(
                "SSRF_TEST_ALLOW_HOSTS must not be set when ENV=production. "
                "Remove SSRF_TEST_ALLOW_HOSTS from the environment before "
                "starting the service in production."
            )
        return self

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    @property
    def ssrf_allowed_hosts(self) -> list[str]:
        """Return the parsed list of SSRF-test-allowed hostnames."""
        if not self.ssrf_test_allow_hosts:
            return []
        return [h.strip() for h in self.ssrf_test_allow_hosts.split(",") if h.strip()]

    @property
    def is_aws(self) -> bool:
        """True when running in AWS (Data API mode), False for local psycopg."""
        return bool(self.db_resource_arn and self.db_secret_arn)


# ---------------------------------------------------------------------------
# Module-level accessor
# ---------------------------------------------------------------------------


@functools.cache
def get_settings() -> Settings:
    """Return the application settings, loading secrets exactly once.

    If ``SECRETS_ARN`` is present in the environment, the named Secrets
    Manager secret is fetched and its values merged into the environment
    before constructing the ``Settings`` object.

    Raises ``RuntimeError`` on a missing or inaccessible secret so that the
    process refuses to start with incomplete configuration.
    Raises ``pydantic.ValidationError`` if the resolved environment values
    violate the Settings constraints (e.g. SSRF allowlist in production).
    """
    secrets_arn = os.environ.get("SECRETS_ARN")
    if secrets_arn:
        logger.info("Loading secrets from Secrets Manager: %s", secrets_arn)
        _load_secrets_into_env(secrets_arn)

    try:
        settings = Settings()
    except ValidationError as exc:
        # Re-raise as RuntimeError so callers (and startup code) see a clear
        # message without having to handle pydantic exceptions.
        raise RuntimeError(f"Invalid configuration: {exc}") from exc

    return settings
