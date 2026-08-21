"""What the Agent hands back to a channel adapter — never a raw LLM string.

`approval_card` is populated only when the Gateway itself returned
APPROVAL_REQUIRED; the Agent cannot manufacture one on its own, since it has
no authority to decide that an approval is needed in the first place.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ApprovalCard(BaseModel):
    approval_id: str
    tool_id: str
    target_summary: str
    reason: str
    risk_level: str
    blast_radius: str
    expires_in_seconds: int = 600


class AgentReply(BaseModel):
    text: str
    status: str = "ok"  # ok | approval_required | denied | error | clarification
    approval_card: ApprovalCard | None = None
    investigation_id: str | None = None
    result_data: dict[str, Any] | None = None
