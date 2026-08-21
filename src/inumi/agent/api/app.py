"""Agent FastAPI app (spec §57).

`POST /v1/chat` is only ever called by a channel adapter that has *already*
verified the sender's enterprise identity and DBA-team membership — but the
Agent (and everything downstream) treats that as convenience, not as a
security boundary: the Gateway independently re-resolves identity from the
same `channel`/`channel_account_id` pair on every tool call regardless
(spec §62).
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from inumi.agent.context_manager import ContextManager
from inumi.agent.llm.provider import build_llm_provider
from inumi.agent.orchestrator import AgentOrchestrator
from inumi.agent.reply import AgentReply
from inumi.agent.tool_client import ToolClient
from inumi.common.config import Settings, get_settings
from inumi.common.observability import configure_logging, get_logger
from inumi.common.service_auth import ServiceTokenIssuer, ServiceTokenVerifier

logger = get_logger(__name__)


class ChatRequest(BaseModel):
    channel: str
    channel_account_id: str
    conversation_id: str
    channel_thread_id: str = ""
    message: str


class ApprovalEventRequest(BaseModel):
    channel: str
    channel_account_id: str
    conversation_id: str
    approval_id: str
    decision: str  # "approve" | "reject"


def create_app(settings: Settings | None = None, *, gateway_transport=None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_for_production()
    configure_logging("agent", settings.log_level)

    issuer = ServiceTokenIssuer(settings.service_jwt_secret, settings.service_jwt_issuer)
    verifier = ServiceTokenVerifier(settings.service_jwt_secret, settings.service_jwt_issuer)
    tool_client = ToolClient(settings.gateway_base_url, issuer, transport=gateway_transport)
    llm = build_llm_provider(settings)
    context = ContextManager()
    orchestrator = AgentOrchestrator(llm, tool_client, context)

    app = FastAPI(title="Inumi AI DBA Agent", version="0.1.0")

    async def require_channel_service_token(x_service_token: str | None = Header(default=None)) -> None:
        if not x_service_token:
            raise HTTPException(status_code=401, detail="Missing service token.")
        try:
            verifier.verify(x_service_token, expected_audience="inumi-agent")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=401, detail="Invalid service token.") from exc

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/chat", response_model=AgentReply, dependencies=[Depends(require_channel_service_token)])
    async def chat(body: ChatRequest) -> AgentReply:
        logger.info("chat_received", channel=body.channel, conversation_id=body.conversation_id)
        return await orchestrator.handle_message(
            channel=body.channel,
            channel_account_id=body.channel_account_id,
            conversation_id=body.conversation_id,
            channel_thread_id=body.channel_thread_id,
            message=body.message,
        )

    @app.post(
        "/v1/chat/events", response_model=AgentReply, dependencies=[Depends(require_channel_service_token)]
    )
    async def chat_events(body: ApprovalEventRequest) -> AgentReply:
        logger.info(
            "approval_event_received", conversation_id=body.conversation_id, decision=body.decision
        )
        return await orchestrator.handle_approval_decision(
            conversation_id=body.conversation_id,
            decision=body.decision,
            channel=body.channel,
            channel_account_id=body.channel_account_id,
        )

    return app


app = create_app()
