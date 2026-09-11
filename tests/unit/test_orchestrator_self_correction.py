"""A DENIED response whose failure_code means "the call was shaped wrong"
(spec §35/§39) must give the LLM a concrete chance to fix it within the
same investigation, instead of ending the turn on a mistake the model
could plausibly correct — reproduces a live finding: a real model omitted
a required target field, then self-corrected immediately once the exact
Gateway rejection was fed back as an observation on the next turn."""

from __future__ import annotations

import pytest

from inumi.agent.context_manager import ConversationState, InvestigationState
from inumi.agent.llm.registry import LLMRegistry
from inumi.agent.orchestrator import AgentOrchestrator
from inumi.agent.planner.actions import ProposeToolCall
from inumi.common.models.tool import ToolCallResponse, ToolCallStatus


class _FakeToolClient:
    def __init__(self, response: ToolCallResponse):
        self.response = response
        self.submit_count = 0

    async def submit(self, request):
        self.submit_count += 1
        return self.response


def _orchestrator(tool_client) -> AgentOrchestrator:
    return AgentOrchestrator(
        llm_registry=LLMRegistry.for_testing(None),  # unused by _submit_and_relay directly
        tool_client=tool_client,
        context=None,  # unused by _submit_and_relay directly
    )


def _state_and_investigation():
    state = ConversationState(
        conversation_id="conv1", channel="dev", channel_thread_id="", channel_account_id="dba_l2@example.com"
    )
    investigation = InvestigationState(investigation_id="inv1", problem="refresh stats", turn_count=0)
    return state, investigation


def _action() -> ProposeToolCall:
    return ProposeToolCall(
        tool_id="database.update_statistics",
        arguments={},
        target={},
        reason="Refreshing stale statistics.",
    )


@pytest.mark.asyncio
async def test_a_self_correctable_denial_continues_the_investigation():
    response = ToolCallResponse(
        status=ToolCallStatus.DENIED,
        failure_code="INVALID_TARGET",
        message="Missing required target field(s) for this operation: schema_name, object_name.",
    )
    client = _FakeToolClient(response)
    orchestrator = _orchestrator(client)
    state, investigation = _state_and_investigation()

    reply = await orchestrator._submit_and_relay(state, investigation, _action(), "dev", "dba_l2@example.com")

    assert reply is None  # loop continues, no reply sent to the DBA yet
    assert investigation.transcript[-1]["result"]["failure_code"] == "INVALID_TARGET"
    assert "INVALID_TARGET" in investigation.evidence[-1]


@pytest.mark.asyncio
async def test_a_permissions_denial_still_ends_the_turn_immediately():
    """UNAUTHORIZED (or POLICY_DENIED, TOOL_NOT_AVAILABLE, ...) is a fact no
    retry with different arguments changes — must not burn the turn budget
    retrying something that can only ever fail the same way."""
    response = ToolCallResponse(
        status=ToolCallStatus.DENIED,
        failure_code="UNAUTHORIZED",
        message="Role DBA_L1 cannot call database.update_statistics.",
    )
    client = _FakeToolClient(response)
    orchestrator = _orchestrator(client)
    state, investigation = _state_and_investigation()

    reply = await orchestrator._submit_and_relay(state, investigation, _action(), "dev", "dba_l1@example.com")

    assert reply is not None
    assert reply.status == "denied"
    assert investigation.transcript == []


@pytest.mark.asyncio
async def test_a_self_correctable_denial_does_not_loop_past_the_turn_budget():
    """Once the turn cap is reached, even a self-correctable failure ends
    the turn immediately — no infinite retry loop."""
    response = ToolCallResponse(
        status=ToolCallStatus.DENIED, failure_code="INVALID_TARGET", message="Missing target fields."
    )
    client = _FakeToolClient(response)
    orchestrator = _orchestrator(client)
    state, investigation = _state_and_investigation()
    investigation.turn_count = 6  # at the cap

    reply = await orchestrator._submit_and_relay(state, investigation, _action(), "dev", "dba_l2@example.com")

    assert reply is not None
    assert reply.status == "denied"


@pytest.mark.asyncio
async def test_a_failed_status_is_logged_as_a_failed_step_not_mislabeled_as_executed():
    """FAILED (an adapter-level failure — spec §8's execution layer, not a
    Gateway policy decision) was previously unhandled here and fell through
    to the EXECUTED branch, logging a failure as if it had succeeded. Playbooks
    make this more likely to surface (they proactively call diagnostics a
    given engine/topology may not implement) — must be recorded plainly and
    the investigation must still be able to continue."""
    response = ToolCallResponse(status=ToolCallStatus.FAILED, message="adapter connection timeout")
    client = _FakeToolClient(response)
    orchestrator = _orchestrator(client)
    state, investigation = _state_and_investigation()

    reply = await orchestrator._submit_and_relay(state, investigation, _action(), "dev", "dba_l2@example.com")

    assert reply is None  # the loop continues — a single failed diagnostic doesn't end the investigation
    assert investigation.transcript[-1]["result"]["error"] == "adapter connection timeout"
    assert "failed" in investigation.evidence[-1].lower()
    assert "adapter connection timeout" in investigation.evidence[-1]
