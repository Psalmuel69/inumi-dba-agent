from __future__ import annotations

import uuid

import pytest

from inumi.common.config import Settings
from inumi.common.ids import new_id
from inumi.common.models.tool import ToolCallRequest, ToolCallStatus
from inumi.execution.credentials.provider import LocalDevCredentialProvider
from inumi.execution.service import ExecutionService
from inumi.gateway.domain.data_policy import DataMinimizer
from inumi.gateway.domain.risk_engine import RiskEngine
from inumi.gateway.domain.tool_call_handler import ToolCallHandler
from inumi.gateway.infrastructure.execution_client import InProcessExecutionClient


def _settings() -> Settings:
    return Settings(_env_file=None, execution_mode="mock")


async def _make_handler(db, tool_registry, inventory, target_validator, policy_engine, rate_limiter):
    settings = _settings()
    execution_service = ExecutionService(
        settings, credential_provider=LocalDevCredentialProvider("config/dev_credentials.yaml")
    )
    execution_client = InProcessExecutionClient(execution_service)
    session_cm = db.session()
    session = await session_cm.__aenter__()
    handler = ToolCallHandler(
        tool_registry=tool_registry,
        inventory=inventory,
        target_validator=target_validator,
        policy_engine=policy_engine,
        risk_engine=RiskEngine(),
        rate_limiter=rate_limiter,
        data_minimizer=DataMinimizer(),
        execution_client=execution_client,
        session=session,
        agent_version="test",
        channel="dev",
        identity_provider_name="mock",
    )
    return handler, session, session_cm


@pytest.mark.asyncio
async def test_read_only_health_check_executes_end_to_end(
    db, tool_registry, inventory, target_validator, policy_engine, rate_limiter, identity_provider
):
    handler, session, cm = await _make_handler(
        db, tool_registry, inventory, target_validator, policy_engine, rate_limiter
    )
    try:
        identity = await identity_provider.resolve_by_external_account("slack", "U_MOCK_L2")
        request = ToolCallRequest(
            tool_id="database.get_health",
            arguments={},
            target={"environment": "production", "database": "CoreBanking"},
            reason="investigating slowness",
            conversation_id="conv_1",
            request_id=new_id("req"),
        )
        response = await handler.handle(identity, request)
        assert response.status == ToolCallStatus.EXECUTED
        assert response.result["rows"][0]["active_sessions"] == 42
    finally:
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_dba_l1_denied_from_killing_session_in_production(
    db, tool_registry, inventory, target_validator, policy_engine, rate_limiter, identity_provider
):
    handler, session, cm = await _make_handler(
        db, tool_registry, inventory, target_validator, policy_engine, rate_limiter
    )
    try:
        identity = await identity_provider.resolve_by_external_account("slack", "U_MOCK_L1")
        request = ToolCallRequest(
            tool_id="database.kill_session",
            arguments={"session_id": "9182", "reason": "blocking chain"},
            target={"environment": "production", "database": "CoreBanking"},
            reason="mitigate blocking",
            conversation_id="conv_2",
            request_id=new_id("req"),
        )
        response = await handler.handle(identity, request)
        assert response.status == ToolCallStatus.DENIED
        assert response.failure_code == "UNAUTHORIZED"
    finally:
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_full_approval_workflow_kill_session(
    db, tool_registry, inventory, target_validator, policy_engine, rate_limiter, identity_provider
):
    """Mirrors the acceptance scenario in spec §54/§68: investigate, propose
    kill_session, require approval, approve, execute, verify."""
    handler, session, cm = await _make_handler(
        db, tool_registry, inventory, target_validator, policy_engine, rate_limiter
    )
    try:
        identity = await identity_provider.resolve_by_external_account("slack", "U_MOCK_L2")
        request = ToolCallRequest(
            tool_id="database.kill_session",
            arguments={"session_id": "9182", "reason": "blocking 43 sessions"},
            target={"environment": "production", "database": "CoreBanking"},
            reason="mitigate blocking",
            conversation_id="conv_3",
            request_id=new_id("req"),
        )
        first = await handler.handle(identity, request)
        assert first.status == ToolCallStatus.APPROVAL_REQUIRED
        approval_id = first.approval_id
        assert approval_id

        approver = await identity_provider.resolve_by_external_account("slack", "U_MOCK_L2")
        from inumi.gateway.domain.approval import ApprovalDecision, ApprovalEngine

        engine = ApprovalEngine(session)
        await engine.decide(
            approval_id=approval_id, approver=approver, decision=ApprovalDecision.APPROVE
        )

        second_request = request.model_copy(
            update={"approval_id": approval_id, "request_id": new_id("req")}
        )
        second = await handler.handle(identity, second_request)
        assert second.status == ToolCallStatus.EXECUTED
        assert second.result["affected"]["terminated"] is True
    finally:
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_execution_denied_if_agent_alters_action_after_approval(
    db, tool_registry, inventory, target_validator, policy_engine, rate_limiter, identity_provider
):
    """The exact scenario from spec §44 run through the full Gateway pipeline."""
    handler, session, cm = await _make_handler(
        db, tool_registry, inventory, target_validator, policy_engine, rate_limiter
    )
    try:
        identity = await identity_provider.resolve_by_external_account("slack", "U_MOCK_L2")
        request = ToolCallRequest(
            tool_id="database.kill_session",
            arguments={"session_id": "9182", "reason": "blocking chain"},
            target={"environment": "production", "database": "CoreBanking"},
            reason="mitigate blocking",
            conversation_id="conv_4",
            request_id=new_id("req"),
        )
        first = await handler.handle(identity, request)
        approval_id = first.approval_id

        from inumi.gateway.domain.approval import ApprovalDecision, ApprovalEngine

        engine = ApprovalEngine(session)
        await engine.decide(
            approval_id=approval_id, approver=identity, decision=ApprovalDecision.APPROVE
        )

        tampered = request.model_copy(
            update={
                "approval_id": approval_id,
                "request_id": new_id("req"),
                "arguments": {"session_id": "9183", "reason": "blocking chain"},
            }
        )
        response = await handler.handle(identity, tampered)
        assert response.status == ToolCallStatus.DENIED
        assert response.failure_code == "APPROVAL_MISMATCH"
    finally:
        await cm.__aexit__(None, None, None)
