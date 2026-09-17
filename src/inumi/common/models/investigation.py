"""Investigation persistence models — the wire contract between the Agent
(which creates/updates these over HTTP as an investigation runs) and the
Gateway (which stores them and serves back memory recall). The Agent has no
database access of its own; everything here travels over `ToolClient`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class InvestigationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    investigation_id: str
    conversation_id: str
    user_subject_id: str
    server_id: str | None = None
    target: dict[str, Any] = Field(default_factory=dict)
    problem: str = ""
    status: str = "INVESTIGATING"


class InvestigationUpdateRequest(BaseModel):
    """All fields optional — a partial update. `None` means "leave
    unchanged," never "clear this field" (nothing here has a legitimate
    reason to be reset to null mid-investigation)."""

    model_config = ConfigDict(extra="ignore")

    server_id: str | None = None
    target: dict[str, Any] | None = None
    status: str | None = None
    evidence: list[Any] | None = None
    hypotheses: list[Any] | None = None
    findings: list[Any] | None = None
    recommendations: list[Any] | None = None
    actions: list[Any] | None = None


class InvestigationEventCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class InvestigationMemoryEntry(BaseModel):
    """A recalled past investigation on the same server — background
    context for the LLM's problem statement, never grounding evidence (see
    `agent.orchestrator._ungrounded_identifiers`'s own docstring for why
    that distinction matters)."""

    model_config = ConfigDict(extra="ignore")

    investigation_id: str
    problem: str
    status: str
    findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    updated_at: dt.datetime
