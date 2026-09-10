"""End-to-end acceptance scenario (spec §68).

A verified DBA_L2 asks about CoreBanking production slowness in Teams; the
Agent (Mock LLM) investigates via the real Gateway pipeline and the mock
Execution Service, proposes killing the head blocker, the Gateway requires
approval, the DBA approves, and the action executes and is reported back —
with every hop actually going through HTTP + the real Gateway pipeline
(policy/risk/approval/execution), just wired via in-process ASGI transports
instead of real sockets.
"""

from __future__ import annotations

import httpx

from inumi.agent.context_manager import ContextManager
from inumi.agent.llm.mock import MockLLMProvider
from inumi.agent.llm.registry import LLMRegistry
from inumi.agent.orchestrator import AgentOrchestrator
from inumi.agent.tool_client import ToolClient
from inumi.common.config import Settings
from inumi.common.service_auth import ServiceTokenIssuer
from inumi.execution.api.app import create_app as create_execution_app
from inumi.gateway.api.app import create_app as create_gateway_app
from tests.canned_adapter import canned_adapter_factory


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        control_db_url="sqlite+aiosqlite:///:memory:",
        service_jwt_secret="test-secret",
        service_jwt_issuer="inumi-internal",
        llm_provider="mock",
    )


async def _build_orchestrator() -> AgentOrchestrator:
    settings = _settings()
    execution_app = create_execution_app(settings, adapter_factory=canned_adapter_factory)
    execution_transport = httpx.ASGITransport(app=execution_app)
    gateway_app = create_gateway_app(settings, execution_transport=execution_transport)
    # httpx.ASGITransport doesn't emit lifespan events, so create the
    # in-memory sqlite schema explicitly (a real server run — or the
    # TestClient-based tests in test_gateway_api.py — trigger this via
    # the app's lifespan instead).
    await gateway_app.state.gateway.db.create_all()
    gateway_transport = httpx.ASGITransport(app=gateway_app)

    issuer = ServiceTokenIssuer(settings.service_jwt_secret, settings.service_jwt_issuer)
    tool_client = ToolClient(
        settings.gateway_base_url, issuer, transport=gateway_transport
    )
    return AgentOrchestrator(
        LLMRegistry.for_testing(MockLLMProvider()), tool_client, ContextManager()
    )


async def test_acceptance_scenario_investigate_approve_execute_verify():
    orchestrator = await _build_orchestrator()

    first = await orchestrator.handle_message(
        channel="teams",
        channel_account_id="aad-mock-l2",
        conversation_id="conv_accept_1",
        channel_thread_id="thread_1",
        message="CoreBanking production is slow. Investigate and tell me what is wrong.",
    )

    assert first.status == "approval_required"
    assert first.approval_card is not None
    assert first.approval_card.tool_id == "database.kill_session"

    second = await orchestrator.handle_approval_decision(
        conversation_id="conv_accept_1",
        decision="approve",
        channel="teams",
        channel_account_id="aad-mock-l2",
    )

    assert second.status == "ok"
    assert "Action approved" in second.text
    assert "MITIGATED" in second.text


async def test_non_dba_task_message_asks_for_clarification():
    orchestrator = await _build_orchestrator()
    reply = await orchestrator.handle_message(
        channel="teams",
        channel_account_id="aad-mock-l2",
        conversation_id="conv_accept_2",
        channel_thread_id="",
        message="What's for lunch?",
    )
    assert reply.status == "clarification"


async def test_greeting_returns_help_text_without_starting_investigation():
    orchestrator = await _build_orchestrator()
    reply = await orchestrator.handle_message(
        channel="teams",
        channel_account_id="aad-mock-l2",
        conversation_id="conv_accept_3",
        channel_thread_id="",
        message="hello",
    )
    assert "Inumi" in reply.text


async def test_l1_user_gets_denied_response_not_a_crash():
    orchestrator = await _build_orchestrator()
    reply = await orchestrator.handle_message(
        channel="teams",
        channel_account_id="aad-mock-l1",
        conversation_id="conv_accept_4",
        channel_thread_id="",
        message="CoreBanking production is slow, kill session 9182",
    )
    # DBA_L1 cannot even reach kill_session on this critical prod database.
    assert reply.status == "denied"
    assert "can't do that" in reply.text
