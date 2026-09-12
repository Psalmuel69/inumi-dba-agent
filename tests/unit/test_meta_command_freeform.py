"""Reproduces a live finding: "List all my servers" (sent as an ordinary
Slack message, not the literal `/servers` command) was treated as a fresh
investigation request and hit the environment-clarification gate for no
reason — the DBA should never have to know or use the exact slash syntax.
`IntentExtraction.meta_command` recognizes these as a request for one of
the agent's own utility actions, and handle_message routes it to the same
handler the exact slash command already uses."""

from __future__ import annotations

import pytest

from inumi.agent.context_manager import ContextManager
from inumi.agent.llm.mock import MockLLMProvider
from inumi.agent.llm.registry import LLMRegistry
from inumi.agent.orchestrator import AgentOrchestrator
from inumi.agent.planner.actions import IntentExtraction


class _FakeToolClient:
    def __init__(self, servers=None):
        self._servers = servers or []

    async def list_servers(self):
        return self._servers

    async def available_tools(self, channel, channel_account_id):
        return []

    async def get_server_catalog(self, server_id):
        return {"server": {"id": server_id}, "catalog": None}

    async def refresh_catalog(self, channel, channel_account_id, server_id=None):
        return {"status": "OK", "server_id": server_id}


class _FakeLLM:
    def __init__(self, intent: IntentExtraction):
        self._intent = intent

    async def extract_intent(self, *args, **kwargs):
        return self._intent


def _orchestrator(llm, servers=None) -> AgentOrchestrator:
    return AgentOrchestrator(
        llm_registry=LLMRegistry.for_testing(llm),
        tool_client=_FakeToolClient(servers),
        context=ContextManager(),
    )


@pytest.mark.asyncio
async def test_a_freeform_servers_request_lists_servers_not_a_fresh_investigation():
    llm = _FakeLLM(IntentExtraction(is_dba_task=False, meta_command="servers"))
    servers = [
        {
            "id": "postgres-local",
            "environment": "development",
            "platform": "postgresql",
            "criticality": "standard",
        }
    ]
    orchestrator = _orchestrator(llm, servers=servers)

    reply = await orchestrator.handle_message(
        channel="slack",
        channel_account_id="U123",
        conversation_id="conv1",
        channel_thread_id="",
        message="List all my servers",
    )

    assert reply.status != "clarification"
    assert "postgres-local" in reply.text


@pytest.mark.asyncio
async def test_a_freeform_playbooks_request_lists_playbooks():
    llm = _FakeLLM(IntentExtraction(is_dba_task=False, meta_command="playbooks"))
    orchestrator = _orchestrator(llm)

    reply = await orchestrator.handle_message(
        channel="slack",
        channel_account_id="U123",
        conversation_id="conv2",
        channel_thread_id="",
        message="what playbooks do you have",
    )

    assert "Slow Query Investigation" in reply.text


@pytest.mark.asyncio
async def test_a_freeform_discover_request_names_the_instance_hint_as_the_target():
    llm = _FakeLLM(
        IntentExtraction(is_dba_task=False, meta_command="discover", instance_hint="postgres-local")
    )
    orchestrator = _orchestrator(llm)

    reply = await orchestrator.handle_message(
        channel="slack", channel_account_id="U123", conversation_id="conv3", channel_thread_id="",
        message="please run discovery on postgres-local",
    )

    assert "Discovery complete" in reply.text
    assert "postgres-local" in reply.text


@pytest.mark.asyncio
async def test_a_freeform_catalog_request_with_no_server_named_asks_which_one():
    llm = _FakeLLM(IntentExtraction(is_dba_task=False, meta_command="catalog", instance_hint=None))
    orchestrator = _orchestrator(llm)

    reply = await orchestrator.handle_message(
        channel="slack", channel_account_id="U123", conversation_id="conv4", channel_thread_id="",
        message="show me the catalog",
    )

    assert "which server" in reply.text.lower()


@pytest.mark.asyncio
async def test_a_freeform_status_request_with_no_investigation_says_so():
    llm = _FakeLLM(IntentExtraction(is_dba_task=False, meta_command="status"))
    orchestrator = _orchestrator(llm)

    reply = await orchestrator.handle_message(
        channel="slack", channel_account_id="U123", conversation_id="conv5", channel_thread_id="",
        message="what's the status",
    )

    assert "no active investigation" in reply.text.lower()


@pytest.mark.asyncio
async def test_a_freeform_approve_with_no_pending_approval_says_so():
    llm = _FakeLLM(IntentExtraction(is_dba_task=False, meta_command="approve"))
    orchestrator = _orchestrator(llm)

    reply = await orchestrator.handle_message(
        channel="slack", channel_account_id="U123", conversation_id="conv6", channel_thread_id="",
        message="go ahead",
    )

    assert "no pending approval" in reply.text.lower()


@pytest.mark.asyncio
async def test_a_freeform_help_request_gets_the_help_text():
    llm = _FakeLLM(IntentExtraction(is_dba_task=False, meta_command="help"))
    orchestrator = _orchestrator(llm)

    reply = await orchestrator.handle_message(
        channel="slack", channel_account_id="U123", conversation_id="conv7", channel_thread_id="",
        message="what can you do",
    )

    assert "Inumi" in reply.text


class TestMockPlannerMetaCommandDetection:
    """The deterministic offline planner recognizes the same free-text
    phrasings, deliberately more conservatively than the real-provider
    prompt (see mock.py's own module comment) to keep this test-only
    planner's false-positive rate low."""

    @pytest.mark.asyncio
    async def test_list_servers_phrasing(self):
        result = await MockLLMProvider().extract_intent("list my servers", known_database_names=[])
        assert result.meta_command == "servers"

    @pytest.mark.asyncio
    async def test_playbooks_phrasing(self):
        result = await MockLLMProvider().extract_intent(
            "show me your playbooks", known_database_names=[]
        )
        assert result.meta_command == "playbooks"

    @pytest.mark.asyncio
    async def test_discover_phrasing(self):
        result = await MockLLMProvider().extract_intent("run discovery please", known_database_names=[])
        assert result.meta_command == "discover"

    @pytest.mark.asyncio
    async def test_an_ordinary_investigation_request_is_not_misdetected_as_meta(self):
        result = await MockLLMProvider().extract_intent(
            "Why is CoreBanking so slow right now?", known_database_names=["CoreBanking"]
        )
        assert result.meta_command is None
        assert result.is_dba_task is True
