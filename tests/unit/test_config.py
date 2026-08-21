"""Fail-closed production configuration checks (spec §63)."""

from __future__ import annotations

import pytest

from inumi.common.config import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_development_settings_never_raise_regardless_of_mock_flags():
    settings = _settings(inumi_env="development")
    settings.validate_for_production()  # should not raise


def test_production_with_all_mock_defaults_refuses_to_start():
    settings = _settings(inumi_env="production")
    with pytest.raises(RuntimeError) as exc:
        settings.validate_for_production()
    message = str(exc.value)
    assert "LLM_PROVIDER=mock" in message
    assert "EXECUTION_MODE=mock" in message
    assert "IDENTITY_PROVIDER=mock" in message
    assert "SECRETS_PROVIDER=local_dev" in message
    assert "SERVICE_JWT_SECRET" in message


def test_production_with_every_flag_properly_set_does_not_raise():
    settings = _settings(
        inumi_env="production",
        llm_provider="anthropic",
        anthropic_api_key="sk-ant-real-key",
        execution_mode="real",
        identity_provider="oidc",
        secrets_provider="vault",
        service_jwt_secret="a-real-unique-production-secret",
    )
    settings.validate_for_production()  # should not raise


def test_production_flags_only_one_violation_at_a_time():
    settings = _settings(
        inumi_env="production",
        llm_provider="mock",
        execution_mode="real",
        identity_provider="oidc",
        secrets_provider="vault",
        service_jwt_secret="a-real-unique-production-secret",
    )
    with pytest.raises(RuntimeError) as exc:
        settings.validate_for_production()
    message = str(exc.value)
    assert "LLM_PROVIDER=mock" in message
    assert "EXECUTION_MODE=mock" not in message
    assert "IDENTITY_PROVIDER=mock" not in message
