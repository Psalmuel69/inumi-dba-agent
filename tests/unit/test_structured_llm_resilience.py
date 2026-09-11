"""StructuredLLMProvider (the shared plumbing behind Anthropic/OpenAI/Gemini/
DeepSeek) must never let a provider outage or a malformed completion crash
the chat request — both degrade to a clear response instead (spec §34).

Reproduces two real failures found running live against Gemini 3.5 Flash:
a transient 503 propagating as an unhandled exception, and a read-only
`propose_tool_call` completion missing `reason` (a Pydantic-required field
the flat cross-provider schema can't mark conditionally-required)."""

from __future__ import annotations

from typing import Any

import pytest

from inumi.agent.llm.base import StructuredLLMProvider
from inumi.agent.planner.actions import AskClarification, IntentExtraction, ProposeToolCall


class _FakeStructuredProvider(StructuredLLMProvider):
    provider_name = "fake"
    _CALL_RETRY_DELAY_SECONDS = 0  # keep the retry-on-failure tests instant

    def __init__(self, *, tool_result: dict[str, Any] | None = None, tool_error: Exception | None = None):
        super().__init__("fake-model")
        self._tool_result = tool_result
        self._tool_error = tool_error
        self.call_count = 0

    async def _call_tool(self, *, system: str, user: str, schema: dict, tool_name: str) -> dict:
        self.call_count += 1
        if self._tool_error is not None:
            raise self._tool_error
        return self._tool_result or {}

    async def _call_text(self, *, system: str, user: str) -> str:
        return ""


@pytest.mark.asyncio
async def test_decide_next_action_degrades_on_a_transient_provider_outage():
    provider = _FakeStructuredProvider(tool_error=RuntimeError("503 UNAVAILABLE: high demand"))
    action = await provider.decide_next_action(
        problem_statement="check health", available_tool_ids=["database.get_health"],
        transcript=[], turn_count=0,
    )
    assert isinstance(action, AskClarification)
    assert "fake" in action.question and "try again" in action.question.lower()
    # First attempt + _CALL_RETRIES retries, all failing the same way.
    assert provider.call_count == provider._CALL_RETRIES + 1


@pytest.mark.asyncio
async def test_decide_next_action_recovers_after_a_transient_failure():
    """A call that fails once and then succeeds must not be treated as a
    permanent outage — this is the whole point of retrying."""

    class _FlakyThenOk(_FakeStructuredProvider):
        async def _call_tool(self, *, system, user, schema, tool_name):
            self.call_count += 1
            if self.call_count == 1:
                raise RuntimeError("503 UNAVAILABLE: high demand")
            return {
                "action": "propose_tool_call",
                "tool_id": "database.get_health",
                "reason": "Baseline check.",
            }

    provider = _FlakyThenOk()
    action = await provider.decide_next_action(
        problem_statement="check health", available_tool_ids=["database.get_health"],
        transcript=[], turn_count=0,
    )
    assert isinstance(action, ProposeToolCall)
    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_decide_next_action_degrades_on_a_malformed_completion():
    provider = _FakeStructuredProvider(
        tool_result={"action": "propose_tool_call", "tool_id": "database.get_health"}
    )
    action = await provider.decide_next_action(
        problem_statement="check health", available_tool_ids=["database.get_health"],
        transcript=[], turn_count=0,
    )
    assert isinstance(action, AskClarification)


@pytest.mark.asyncio
async def test_decide_next_action_accepts_a_read_tool_call_with_reason():
    """The bug this pins: `reason` is Pydantic-required on ProposeToolCall
    for every tool call, read or write — the schema itself can't enforce
    that conditionally, so the system prompt must, and the model must
    actually follow it."""
    provider = _FakeStructuredProvider(
        tool_result={
            "action": "propose_tool_call",
            "tool_id": "database.get_health",
            "reason": "Establishing a baseline before investigating further.",
        }
    )
    action = await provider.decide_next_action(
        problem_statement="check health", available_tool_ids=["database.get_health"],
        transcript=[], turn_count=0,
    )
    assert isinstance(action, ProposeToolCall)
    assert action.tool_id == "database.get_health"


@pytest.mark.asyncio
async def test_extract_intent_degrades_on_a_transient_provider_outage():
    provider = _FakeStructuredProvider(tool_error=RuntimeError("connection reset"))
    intent = await provider.extract_intent("CoreBanking is slow", known_database_names=["CoreBanking"])
    assert isinstance(intent, IntentExtraction)
    assert intent.is_dba_task is True
