"""GET /v1/investigations/{id} (spec §25, §57).

Read-only projection of investigation state maintained by the Agent via
`inumi.gateway.domain.investigation_store` (see Phase 5) — the Gateway
itself never fabricates evidence/findings, it only persists and serves back
what the Agent recorded.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from inumi.gateway.api.deps import get_session, require_agent_service_token
from inumi.gateway.infrastructure.db.models import InvestigationRecord

router = APIRouter(
    prefix="/v1/investigations", tags=["investigations"], dependencies=[Depends(require_agent_service_token)]
)


@router.get("/{investigation_id}")
async def get_investigation(
    investigation_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    record = await session.get(InvestigationRecord, investigation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Investigation not found.")
    return {
        "investigation_id": record.investigation_id,
        "conversation_id": record.conversation_id,
        "target": record.target,
        "problem": record.problem,
        "status": record.status,
        "evidence": record.evidence,
        "hypotheses": record.hypotheses,
        "findings": record.findings,
        "recommendations": record.recommendations,
        "actions": record.actions,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }
