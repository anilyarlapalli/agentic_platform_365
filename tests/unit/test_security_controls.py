from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError

from platform_core.api.policy import policy_for
from platform_core.security.rate_limit import RateLimiter
from platform_core.settings import DEV_JWT_SECRET, Settings

pytestmark = pytest.mark.unit


class _CounterRedis:
    def __init__(self) -> None:
        self.count = 0

    def eval(self, _script: str, _number_of_keys: int, _key: str, window: int):
        self.count += 1
        return [self.count, window]


class _BrokenRedis:
    def eval(self, *_args):
        raise ConnectionError("offline")


def test_deployed_configuration_rejects_exact_development_secret() -> None:
    settings = Settings(
        environment="production",
        service_role="api",
        release="sha-123",
        jwt_secret=DEV_JWT_SECRET,
        telemetry_hmac_key="telemetry-key",
        browser_allowed_origins=["https://console.example.com"],
        database_url="postgresql+psycopg://app:unique@db.example/platform",
        database_relay_url="postgresql+psycopg://relay:unique@db.example/platform",
    )
    assert any("development jwt_secret" in problem for problem in settings.check_coherence())


def test_relay_does_not_require_an_llm_credential() -> None:
    settings = Settings(service_role="relay", llm_provider="openai", openai_api_key=None)
    assert not any("openai_api_key" in problem for problem in settings.check_coherence())


def test_scheduler_production_role_needs_only_its_own_runtime_secrets() -> None:
    settings = Settings(
        environment="production",
        service_role="scheduler",
        release="sha-123",
        otlp_endpoint="https://otel.example:4317",
        telemetry_hmac_key="scheduler-telemetry-key",
        llm_provider="openai",
        openai_api_key=None,
    )
    assert settings.check_coherence() == []


def test_production_api_rejects_wildcard_proxy_trust() -> None:
    settings = Settings(
        environment="production",
        service_role="api",
        release="sha-123",
        jwt_secret="production-jwt-secret-at-least-32-bytes",
        telemetry_hmac_key="api-telemetry-key",
        otlp_endpoint="https://otel.example:4317",
        browser_allowed_origins=["https://console.example.com"],
        trusted_proxy_ips="*",
        database_url="postgresql+psycopg://app:unique@db.example/platform",
        s3_access_key="unique-access",
        s3_secret_key="unique-secret",
        openai_api_key="provider-secret",
    )
    assert any("trusted_proxy_ips='*'" in problem for problem in settings.check_coherence())


def test_production_maintenance_rejects_development_relay_credential() -> None:
    settings = Settings(
        environment="production",
        service_role="maintenance",
        release="sha-123",
        telemetry_hmac_key="maintenance-telemetry-key",
        otlp_endpoint="https://otel.example:4317",
    )
    assert any(
        "development relay database password" in problem
        for problem in settings.check_coherence()
    )


def test_document_ingest_resource_comes_from_body_only() -> None:
    policy = policy_for("POST", "/api/documents")
    assert policy is not None
    assert policy.resource_param == "collection"
    assert policy.resource_location == "body"


def test_distributed_limit_refuses_after_the_window_quota() -> None:
    settings = Settings(environment="local", llm_provider="ollama")
    limiter = RateLimiter(settings, client=_CounterRedis())
    assert limiter.check("login", "subject", limit=2, window_seconds=60).allowed
    assert limiter.check("login", "subject", limit=2, window_seconds=60).allowed
    denied = limiter.check("login", "subject", limit=2, window_seconds=60)
    assert not denied.allowed
    assert denied.remaining == 0
    assert denied.retry_after_seconds == 60


def test_local_limiter_has_a_bounded_fail_safe_when_redis_is_absent() -> None:
    settings = Settings(environment="local", llm_provider="ollama")
    limiter = RateLimiter(settings, client=_BrokenRedis())
    assert limiter.check("api", "subject", limit=1, window_seconds=60).allowed
    assert not limiter.check("api", "subject", limit=1, window_seconds=60).allowed
