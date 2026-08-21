from __future__ import annotations

import pytest

from inumi.common.config import Settings
from inumi.common.models.execution import ExecutionRequest
from inumi.common.models.target import Platform
from inumi.execution.service import ExecutionService


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.mark.asyncio
async def test_mock_mode_health_check_succeeds_without_any_credential():
    settings = _settings(execution_mode="mock")
    service = ExecutionService(settings, credential_provider=None)  # type: ignore[arg-type]
    request = ExecutionRequest(
        execution_id="exec_1",
        tool_id="database.get_health",
        tool_version="1.0.0",
        platform=Platform.SQLSERVER,
        database_id="corebanking-prd-01",
        instance="corebanking-prd-01",
        database="CoreBanking",
        arguments={},
        max_execution_time=30,
        max_result_rows=100,
    )
    result = await service.execute(request)
    assert result.success is True
    assert result.rows[0]["active_sessions"] == 42


@pytest.mark.asyncio
async def test_row_cap_is_enforced_and_reported():
    settings = _settings(execution_mode="mock")
    service = ExecutionService(settings, credential_provider=None)  # type: ignore[arg-type]
    request = ExecutionRequest(
        execution_id="exec_2",
        tool_id="database.get_blocking_sessions",
        tool_version="1.0.0",
        platform=Platform.SQLSERVER,
        database_id="corebanking-prd-01",
        instance="corebanking-prd-01",
        database="CoreBanking",
        arguments={},
        max_execution_time=30,
        max_result_rows=5,
    )
    result = await service.execute(request)
    assert result.success is True
    assert result.row_count == 5
    assert result.truncated is True


@pytest.mark.asyncio
async def test_unknown_tool_id_fails_without_touching_any_database():
    settings = _settings(execution_mode="mock")
    service = ExecutionService(settings, credential_provider=None)  # type: ignore[arg-type]
    request = ExecutionRequest(
        execution_id="exec_3",
        tool_id="database.does_not_exist",
        tool_version="1.0.0",
        platform=Platform.SQLSERVER,
        database_id="corebanking-prd-01",
        instance="corebanking-prd-01",
        database="CoreBanking",
        arguments={},
        max_execution_time=30,
        max_result_rows=100,
    )
    result = await service.execute(request)
    assert result.success is False
    assert result.error_code == "TOOL_NOT_FOUND"


@pytest.mark.asyncio
async def test_execution_timeout_is_reported_not_hung(monkeypatch):
    import asyncio

    settings = _settings(execution_mode="mock")
    service = ExecutionService(settings, credential_provider=None)  # type: ignore[arg-type]

    async def _slow_dispatch(adapter, request):
        await asyncio.sleep(10)

    monkeypatch.setattr(service, "_dispatch", _slow_dispatch)
    request = ExecutionRequest(
        execution_id="exec_4",
        tool_id="database.get_health",
        tool_version="1.0.0",
        platform=Platform.SQLSERVER,
        database_id="corebanking-prd-01",
        instance="corebanking-prd-01",
        database="CoreBanking",
        arguments={},
        max_execution_time=1,
        max_result_rows=100,
    )
    result = await service.execute(request)
    assert result.success is False
    assert result.error_code == "EXECUTION_TIMEOUT"


@pytest.mark.asyncio
async def test_real_mode_without_configured_credentials_fails_closed():
    """A misconfigured secrets provider must never fall back to executing
    anyway — it fails closed (spec §63)."""
    settings = _settings(execution_mode="real", secrets_provider="vault")
    from inumi.execution.credentials.provider import build_credential_provider

    service = ExecutionService(settings, credential_provider=build_credential_provider(settings))
    request = ExecutionRequest(
        execution_id="exec_5",
        tool_id="database.get_health",
        tool_version="1.0.0",
        platform=Platform.SQLSERVER,
        database_id="corebanking-prd-01",
        instance="corebanking-prd-01",
        database="CoreBanking",
        arguments={},
        max_execution_time=5,
        max_result_rows=100,
    )
    result = await service.execute(request)
    assert result.success is False
    assert result.error_code == "EXECUTION_FAILED"
