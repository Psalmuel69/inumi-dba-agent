"""Two live findings from real Slack usage against `handle_message`:

1. Naming a specific, registered server ("check overall health on
   postgres-local") still asked "which environment should I investigate?"
   even though a registered server has exactly one environment in
   config/servers.yaml — asking again for something already implied by the
   instance was never necessary.

2. Answering that clarification with a bare "development" (plus Slack's
   own mention markup: "development <@U0BOTID>") got classified by the
   real model as chitchat, silently discarding the DBA's answer and the
   in-progress investigation — replying with the generic help text
   instead of continuing. `_ENVIRONMENT_ANSWER_RE`'s fast path exists
   specifically so this one clarification (tracked as actual state, not
   free LLM text) can be answered without ever risking that
   misclassification — proven here by a fake LLM whose extract_intent
   raises if it's ever called for this case."""

from __future__ import annotations

import pytest

from inumi.agent.context_manager import ContextManager, InvestigationState
from inumi.agent.llm.registry import LLMRegistry
from inumi.agent.orchestrator import AgentOrchestrator
from inumi.agent.planner.actions import Conclude, IntentExtraction


class _FakeToolClient:
    def __init__(self, servers: list[dict] | None = None):
        self._servers = servers or []

    async def list_servers(self):
        return self._servers

    async def available_tools(self, channel, channel_account_id):
        return []

    async def submit(self, request):
        raise AssertionError("no tool call expected in these tests")


class _FakeLLM:
    """`extract_intent` raises by default — set `intent` to make it return
    something instead. Proves the fast path never calls it when unset."""

    def __init__(self, *, intent: IntentExtraction | None = None, decide_action=None):
        self._intent = intent
        self._decide_action = decide_action or Conclude(summary="Nothing wrong found.")
        self.extract_intent_calls = 0
        self.decide_calls = 0

    async def extract_intent(self, *args, **kwargs):
        self.extract_intent_calls += 1
        if self._intent is None:
            raise AssertionError("extract_intent must not be called here")
        return self._intent

    async def decide_next_action(self, **kwargs):
        self.decide_calls += 1
        return self._decide_action


def _orchestrator(llm, servers=None) -> tuple[AgentOrchestrator, ContextManager]:
    context = ContextManager()
    orchestrator = AgentOrchestrator(
        llm_registry=LLMRegistry.for_testing(llm),
        tool_client=_FakeToolClient(servers),
        context=context,
    )
    return orchestrator, context


@pytest.mark.asyncio
async def test_a_bare_environment_answer_continues_without_calling_extract_intent():
    llm = _FakeLLM()  # extract_intent raises if ever called
    orchestrator, context = _orchestrator(llm)
    state = context.get_or_create("conv1", "slack", "thread1", "U123")
    state.investigation = InvestigationState(
        investigation_id="inv1", problem="check overall health on postgres-local"
    )

    reply = await orchestrator.handle_message(
        channel="slack",
        channel_account_id="U123",
        conversation_id="conv1",
        channel_thread_id="thread1",
        message="development <@U0BOTID>",  # exact live shape: answer + mention noise
    )

    assert llm.extract_intent_calls == 0
    assert state.database_context["environment"] == "development"
    assert state.investigation.status == "CONCLUDED"
    assert reply.status == "ok"
    assert "Nothing wrong found" in reply.text


@pytest.mark.asyncio
async def test_the_fast_path_never_fires_without_a_pending_investigation():
    """A bare "development"-shaped message with no active investigation is
    just a normal message — must go through real intent extraction, not
    be silently swallowed by the fast path."""
    llm = _FakeLLM(intent=IntentExtraction(is_dba_task=False, is_greeting_or_chitchat=True))
    orchestrator, context = _orchestrator(llm)

    reply = await orchestrator.handle_message(
        channel="slack",
        channel_account_id="U123",
        conversation_id="conv2",
        channel_thread_id="",
        message="development",
    )

    assert llm.extract_intent_calls == 1
    assert "Inumi" in reply.text


@pytest.mark.asyncio
async def test_a_resumed_investigation_never_reclassifies_even_with_environment_already_set():
    """The generalized fix, live-reproduced with a *second* clarification
    kind: environment already set, but the DBA's reply ("postgres-local")
    is answering a freeform AskClarification the LLM itself asked ("which
    server?") — this must resume directly (threaded via
    investigation.last_message) exactly like the environment-answer case,
    not just for that one hardcoded gate."""
    llm = _FakeLLM()  # extract_intent raises if ever called
    orchestrator, context = _orchestrator(llm)
    state = context.get_or_create("conv3", "slack", "", "U123")
    state.investigation = InvestigationState(investigation_id="inv1", problem="check health")
    state.database_context["environment"] = "development"

    await orchestrator.handle_message(
        channel="slack",
        channel_account_id="U123",
        conversation_id="conv3",
        channel_thread_id="",
        message="postgres-local",
    )

    assert llm.extract_intent_calls == 0
    assert llm.decide_calls == 1
    assert state.investigation.last_message == ""  # consumed by that one decide_next_action call


@pytest.mark.asyncio
async def test_naming_a_registered_server_auto_resolves_its_environment():
    intent = IntentExtraction(
        is_dba_task=True,
        instance_hint="postgres-local",
        problem_summary="check overall health on postgres-local",
    )
    llm = _FakeLLM(intent=intent)
    servers = [{"id": "postgres-local", "aliases": ["local"], "environment": "development"}]
    orchestrator, context = _orchestrator(llm, servers=servers)

    reply = await orchestrator.handle_message(
        channel="slack",
        channel_account_id="U123",
        conversation_id="conv4",
        channel_thread_id="",
        message="check overall health on postgres-local",
    )

    assert reply.status != "clarification"
    state = context.get_or_create("conv4", "slack", "", "U123")
    assert state.database_context["environment"] == "development"


@pytest.mark.asyncio
async def test_an_explicit_environment_is_never_overridden_by_auto_resolution():
    intent = IntentExtraction(
        is_dba_task=True,
        instance_hint="postgres-local",
        environment_hint="uat",
        problem_summary="check overall health on postgres-local uat",
    )
    llm = _FakeLLM(intent=intent)
    servers = [{"id": "postgres-local", "aliases": [], "environment": "development"}]
    orchestrator, context = _orchestrator(llm, servers=servers)

    await orchestrator.handle_message(
        channel="slack",
        channel_account_id="U123",
        conversation_id="conv5",
        channel_thread_id="",
        message="check overall health on postgres-local uat",
    )

    state = context.get_or_create("conv5", "slack", "", "U123")
    assert state.database_context["environment"] == "uat"


@pytest.mark.asyncio
async def test_an_unregistered_instance_still_asks_for_clarification():
    intent = IntentExtraction(is_dba_task=True, problem_summary="check health on some-unknown-box")
    llm = _FakeLLM(intent=intent)
    orchestrator, context = _orchestrator(llm, servers=[])

    reply = await orchestrator.handle_message(
        channel="slack",
        channel_account_id="U123",
        conversation_id="conv6",
        channel_thread_id="",
        message="check health on some-unknown-box",
    )

    assert reply.status == "clarification"


def test_the_dbas_last_message_is_threaded_into_the_llm_prompt():
    from inumi.agent.context_manager import InvestigationState

    investigation = InvestigationState(investigation_id="inv1", problem="check health")
    assert "postgres-local" not in AgentOrchestrator._problem_statement_for_llm(investigation)

    investigation.last_message = "postgres-local"
    problem = AgentOrchestrator._problem_statement_for_llm(investigation)
    assert "The DBA just replied" in problem
    assert "postgres-local" in problem


def test_environment_for_instance_matches_id_or_alias_case_insensitively():
    servers = [{"id": "postgres-local", "aliases": ["local", "mylocal"], "environment": "development"}]
    orchestrator = AgentOrchestrator(
        llm_registry=LLMRegistry.for_testing(_FakeLLM()),
        tool_client=_FakeToolClient(servers),
        context=ContextManager(),
    )
    import asyncio

    assert asyncio.run(orchestrator._environment_for_instance("Postgres-Local")) == "development"
    assert asyncio.run(orchestrator._environment_for_instance("MyLocal")) == "development"
    assert asyncio.run(orchestrator._environment_for_instance("nope")) is None
