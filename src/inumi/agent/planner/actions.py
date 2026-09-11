"""Structured Agent decisions (spec §35, §36).

Every decision the LLM makes is forced into one of these typed shapes and
validated with Pydantic before the Agent acts on it. There is no code path
where raw LLM text is parsed for an intent and executed directly — a
malformed or nonsensical completion fails validation and the Agent falls
back to asking the user for clarification, it never guesses.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter


class AskClarification(BaseModel):
    action: Literal["ask_clarification"] = "ask_clarification"
    question: str


class ProposeToolCall(BaseModel):
    """The LLM's *proposal* only — tool_id and arguments are re-validated by
    the Gateway from scratch; this model just shapes what the Agent forwards
    as a request. The LLM cannot invent a tool_id outside the list of tools
    it was offered (see `agent.tool_client.ToolClient.available_tools`)."""

    action: Literal["propose_tool_call"] = "propose_tool_call"
    tool_id: str
    arguments: dict = Field(default_factory=dict)
    target: dict = Field(default_factory=dict)
    reason: str


class RecordObservation(BaseModel):
    action: Literal["record_observation"] = "record_observation"
    text: str


class Conclude(BaseModel):
    action: Literal["conclude"] = "conclude"
    summary: str
    likely_root_cause: str | None = None
    confidence: Literal["confirmed", "likely", "unable_to_confirm"] = "unable_to_confirm"
    recommendation: str | None = None
    recommended_tool_call: ProposeToolCall | None = None


AgentAction = Annotated[
    AskClarification | ProposeToolCall | RecordObservation | Conclude,
    Field(discriminator="action"),
]

agent_action_adapter: TypeAdapter = TypeAdapter(AgentAction)


class IntentExtraction(BaseModel):
    """First-pass classification of an incoming message (spec §6, §39)."""

    is_dba_task: bool
    is_greeting_or_chitchat: bool = False
    database_hint: str | None = None
    environment_hint: str | None = None
    # A registered server id/alias explicitly named in the message (e.g. "on
    # postgres-local"). Only ever a value from the known-servers list handed
    # to the extractor — never invented — and only narrows a server the
    # Gateway would otherwise consider ambiguous; it carries no authority of
    # its own (the Gateway independently re-resolves and validates it).
    instance_hint: str | None = None
    problem_summary: str = ""

