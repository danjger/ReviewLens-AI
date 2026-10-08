"""Unit tests for app.core.config.

Covers:
- Default values for all limit fields
- Overriding values via environment variables
- Secrets Manager loading (mocked with moto)
- Production refusal when SSRF_TEST_ALLOW_HOSTS is set
- Cached accessor cache_clear behaviour
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from typing import Any

import boto3
import pytest
from app.core.config import Settings, _load_secrets_into_env, get_settings
from moto import mock_aws

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_secret(name: str, values: dict[str, Any]) -> str:
    """Create a Secrets Manager secret and return its ARN."""
    client = boto3.client("secretsmanager", region_name="us-east-1")
    response = client.create_secret(Name=name, SecretString=json.dumps(values))
    return str(response["ARN"])


# ``.env`` on a developer's machine (and the Compose shell) sets values like
# ``ENV=local`` and ``SSRF_TEST_ALLOW_HOSTS=fixtures``. ``Settings`` reads that
# dotenv file by default, so a plain ``Settings()`` in a "defaults" test would
# assert against the developer's local values rather than the code's declared
# defaults. ``default_settings()`` builds a hermetic ``Settings`` for the
# ``TestDefaults`` class: ``_env_file=None`` disables the dotenv source, and the
# autouse fixture below clears the colliding OS env vars so an exported value in
# the shell can't leak in either. (Secrets-Manager / override / production tests
# deliberately keep the normal construction so they exercise real precedence.)
_DOTENV_OVERRIDE_VARS = (
    "ENV",
    "APP_ENV",
    "SSRF_TEST_ALLOW_HOSTS",
)


def default_settings() -> Settings:
    """Build a Settings ignoring the on-disk .env, for default-value assertions."""
    return Settings(_env_file=None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Generator[None, None, None]:
    """Ensure get_settings() cache is cleared before and after every test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


class TestDefaults:
    """All limit fields must have the correct documented defaults."""

    @pytest.fixture(autouse=True)
    def _isolate_from_dotenv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clear env vars the local .env/shell export that would mask a default.

        Combined with :func:`default_settings` (``_env_file=None``), this makes
        every default-value assertion independent of the developer's ``.env``
        and shell, so the suite is identical locally and in CI.
        """
        for name in _DOTENV_OVERRIDE_VARS:
            monkeypatch.delenv(name, raising=False)

    def test_crawl_limits(self) -> None:
        s = default_settings()
        assert s.max_pages == 10
        assert s.max_reviews == 1000
        assert s.max_urls_per_check == 10
        assert s.check_timeout_s == 60
        assert s.max_upload_mb == 10

    def test_viability_default(self) -> None:
        assert default_settings().viability_min_reviews == 5

    def test_extraction_defaults(self) -> None:
        s = default_settings()
        assert s.extraction_strategy == "auto"
        assert s.extract_page_token_budget == 30_000
        assert s.selector_min_agreement == 0.8

    def test_chat_default(self) -> None:
        assert default_settings().chat_corpus_token_budget == 150_000

    def test_rate_limit_defaults(self) -> None:
        s = default_settings()
        assert s.rl_checks_per_ip_hour == 30
        assert s.rl_uploads_per_ip_hour == 30
        assert s.rl_questions_per_ip_hour == 120
        assert s.rl_global_ai_calls_per_hour == 2000

    def test_sweeper_defaults(self) -> None:
        s = default_settings()
        assert s.sweep_requested_after_min == 5
        assert s.sweep_processing_stale_min == 20

    def test_env_default(self) -> None:
        assert default_settings().env == "development"

    def test_ssrf_allow_hosts_default_empty(self) -> None:
        s = default_settings()
        assert s.ssrf_test_allow_hosts == ""
        assert s.ssrf_allowed_hosts == []

    def test_tracking_params_default_non_empty(self) -> None:
        params = default_settings().tracking_params
        assert "utm_source" in params
        assert "fbclid" in params

    def test_is_aws_false_without_data_api_config(self) -> None:
        assert default_settings().is_aws is False


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------


class TestOverrides:
    """Environment variables must override every setting."""

    def test_override_max_pages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAX_PAGES", "5")
        assert Settings().max_pages == 5

    def test_override_max_reviews(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAX_REVIEWS", "500")
        assert Settings().max_reviews == 500

    def test_override_rl_checks_per_ip_hour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RL_CHECKS_PER_IP_HOUR", "10")
        assert Settings().rl_checks_per_ip_hour == 10

    def test_override_rl_uploads_per_ip_hour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RL_UPLOADS_PER_IP_HOUR", "7")
        assert Settings().rl_uploads_per_ip_hour == 7

    def test_override_claude_chat_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CHAT_MODEL", "claude-3-opus-20240229")
        assert Settings().claude_chat_model == "claude-3-opus-20240229"

    def test_override_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENV", "test")
        assert Settings().env == "test"

    def test_override_s3_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("S3_BUCKET", "my-test-bucket")
        assert Settings().s3_bucket == "my-test-bucket"

    def test_override_check_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHECK_TIMEOUT_S", "30")
        assert Settings().check_timeout_s == 30

    def test_is_aws_true_with_data_api_arns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_RESOURCE_ARN", "arn:aws:rds:us-east-1:123:cluster:x")
        monkeypatch.setenv("DB_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:x")
        assert Settings().is_aws is True

    def test_ssrf_allowed_hosts_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", "fixtures,localhost")
        s = Settings()
        assert s.ssrf_allowed_hosts == ["fixtures", "localhost"]

    def test_ssrf_allowed_hosts_single(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", "fixtures")
        assert Settings().ssrf_allowed_hosts == ["fixtures"]


# ---------------------------------------------------------------------------
# Production SSRF allowlist refusal
# ---------------------------------------------------------------------------


class TestProductionSsrfGuard:
    """The process must refuse to start in production with an SSRF allowlist."""

    def test_raises_when_ssrf_set_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENV", "production")
        monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", "fixtures")
        with pytest.raises(ValueError, match="SSRF_TEST_ALLOW_HOSTS"):
            Settings()

    def test_get_settings_raises_runtime_error_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_settings() must wrap the ValidationError as RuntimeError."""
        monkeypatch.setenv("ENV", "production")
        monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", "fixtures")
        with pytest.raises(RuntimeError, match="SSRF_TEST_ALLOW_HOSTS"):
            get_settings()

    def test_ok_when_ssrf_empty_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENV", "production")
        monkeypatch.delenv("SSRF_TEST_ALLOW_HOSTS", raising=False)
        s = Settings()
        assert s.env == "production"
        assert s.ssrf_test_allow_hosts == ""

    def test_ok_when_ssrf_set_in_development(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENV", "development")
        monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", "fixtures")
        s = Settings()
        assert s.ssrf_test_allow_hosts == "fixtures"

    def test_ok_when_ssrf_set_in_test_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENV", "test")
        monkeypatch.setenv("SSRF_TEST_ALLOW_HOSTS", "fixtures")
        s = Settings()
        assert s.ssrf_test_allow_hosts == "fixtures"


# ---------------------------------------------------------------------------
# Secrets Manager loading
# ---------------------------------------------------------------------------


class TestSecretsManagerLoading:
    """_load_secrets_into_env() must merge secret values into os.environ."""

    @mock_aws
    def test_secret_values_injected_into_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        arn = _make_secret("reviewlens/test", {"S3_BUCKET": "secret-bucket"})
        monkeypatch.delenv("S3_BUCKET", raising=False)

        _load_secrets_into_env(arn)

        assert os.environ["S3_BUCKET"] == "secret-bucket"
        # Clean up what _load_secrets_into_env injected
        monkeypatch.delenv("S3_BUCKET", raising=False)

    @mock_aws
    def test_existing_env_var_not_overwritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit env vars must take priority over secrets."""
        arn = _make_secret("reviewlens/test2", {"S3_BUCKET": "secret-bucket"})
        monkeypatch.setenv("S3_BUCKET", "explicit-bucket")

        _load_secrets_into_env(arn)

        assert os.environ["S3_BUCKET"] == "explicit-bucket"

    @mock_aws
    def test_get_settings_loads_secret_via_secrets_arn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When SECRETS_ARN is set, get_settings() must load the secret."""
        arn = _make_secret(
            "reviewlens/test3",
            {"MAX_PAGES": "7", "S3_BUCKET": "from-secret"},
        )
        monkeypatch.setenv("SECRETS_ARN", arn)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.delenv("MAX_PAGES", raising=False)
        monkeypatch.delenv("S3_BUCKET", raising=False)

        settings = get_settings()

        assert settings.max_pages == 7
        assert settings.s3_bucket == "from-secret"

    @mock_aws
    def test_missing_secret_raises_runtime_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing or inaccessible secret must raise RuntimeError."""
        # Use a non-existent ARN – moto will raise ClientError
        monkeypatch.setenv(
            "SECRETS_ARN",
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:does-not-exist",
        )
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        with pytest.raises(RuntimeError, match="Failed to fetch secret"):
            get_settings()


# ---------------------------------------------------------------------------
# Validator edge cases
# ---------------------------------------------------------------------------


class TestFieldValidators:
    """Pydantic field validators must enforce value constraints."""

    def test_selector_agreement_must_be_positive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SELECTOR_MIN_AGREEMENT", "0.0")
        with pytest.raises(ValueError):
            Settings()

    def test_selector_agreement_allows_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SELECTOR_MIN_AGREEMENT", "1.0")
        assert Settings().selector_min_agreement == 1.0

    def test_selector_agreement_rejects_above_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SELECTOR_MIN_AGREEMENT", "1.1")
        with pytest.raises(ValueError):
            Settings()
