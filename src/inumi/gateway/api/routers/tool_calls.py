"""POST /v1/tool-calls (spec §57).

The single entry point for every tool the Agent invokes. Everything else in
the Gateway (registry, target validation, authorization, policy, risk,
approval, execution, masking, audit) is reached only through
`ToolCallHandler.handle`, called here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from inumi.common.models.tool import ToolCallRequest, ToolCallResponse
from inumi.gateway.api.deps import (
    get_session,
    get_state,
    require_agent_service_token,
    resolve_identity,
)
from inumi.gateway.api.state import GatewayState
from inumi.gateway.domain.tool_call_handler import ToolCallHandler

router = APIRouter(prefix="/v1/tool-calls", tags=["tool-calls"], dependencies=[Depends(require_agent_service_token)])


@router.post("", response_model=ToolCallResponse)
async def submit_tool_call(
    body: ToolCallRequest,
    state: GatewayState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
) -> ToolCallResponse:
    identity = await resolve_identity(state, body.channel, body.channel_account_id)
    handler = ToolCallHandler(
        tool_registry=state.tool_registry,
        inventory=state.inventory,
        target_validator=state.target_validator,
        policy_engine=state.policy_engine,
        risk_engine=state.risk_engine,
        rate_limiter=state.rate_limiter,
        data_minimizer=state.data_minimizer,
        execution_client=state.execution_client,
        session=session,
        agent_version="agent/0.1.0",
        channel=body.channel,
        identity_provider_name=state.settings.identity_provider,
    )
    response = await handler.handle(identity, body)
    await session.commit()
    return response
