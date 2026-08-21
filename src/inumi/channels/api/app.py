"""Channels service FastAPI app (spec §4, §57, §66).

Hosts the Slack and Microsoft Teams webhook adapters plus a `/dev/chat`
mock channel for local development/testing without real Slack/Teams
credentials. Every path here does the same five things before anything
reaches the Agent:

  1. verify webhook/signature (or bot token)
  2. identify the raw channel account
  3. resolve enterprise identity (UX-level check via IdentityProvider)
  4. verify DBA membership (UX-level check — Gateway re-verifies for real)
  5. forward to the Agent's `/v1/chat`, then render the reply back

Channel adapters never execute a database operation and never talk to the
Gateway or Execution Service directly (spec §4).
"""

from __future__ import annotations

import json

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from inumi.agent.reply import AgentReply
from inumi.channels.slack.blocks import render_reply_blocks
from inumi.channels.slack.sender import SlackMessageSender
from inumi.channels.slack.signature import SlackSignatureError, verify_slack_signature
from inumi.channels.teams.auth import BotFrameworkJWTVerifier, DevTeamsAuthVerifier, TeamsAuthError
from inumi.channels.teams.cards import render_reply_card
from inumi.channels.teams.sender import TeamsMessageSender
from inumi.common.config import Settings, get_settings
from inumi.common.identity import MockIdentityProvider
from inumi.common.observability import configure_logging, get_logger
from inumi.common.service_auth import ServiceTokenIssuer

logger = get_logger(__name__)


class DevChatRequest(BaseModel):
    user: str  # the mock directory's `channel_accounts.dev` value, e.g. "dba_l2@example.com"
    message: str
    conversation_id: str = "dev-conversation"


def create_app(settings: Settings | None = None, *, agent_transport=None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging("channels", settings.log_level)

    identity_provider = MockIdentityProvider(settings.identity_config_path)
    issuer = ServiceTokenIssuer(settings.service_jwt_secret, settings.service_jwt_issuer)
    slack_sender = SlackMessageSender(settings.slack_bot_token)

    async def _no_connector_token() -> str | None:
        return None

    teams_sender = TeamsMessageSender(_no_connector_token)
    teams_verifier = (
        BotFrameworkJWTVerifier(settings.teams_app_id)
        if settings.is_production()
        else DevTeamsAuthVerifier(settings.teams_app_password)
    )

    app = FastAPI(title="Inumi Channel Adapters", version="0.1.0")

    async def _call_agent_chat(
        *, channel: str, channel_account_id: str, conversation_id: str, channel_thread_id: str, message: str
    ) -> dict:
        token = issuer.issue(service_name="channels", audience="inumi-agent")
        async with httpx.AsyncClient(
            base_url=settings.agent_base_url,
            transport=agent_transport,
            timeout=60,
        ) as client:
            response = await client.post(
                "/v1/chat",
                headers={"X-Service-Token": token},
                json={
                    "channel": channel,
                    "channel_account_id": channel_account_id,
                    "conversation_id": conversation_id,
                    "channel_thread_id": channel_thread_id,
                    "message": message,
                },
            )
            response.raise_for_status()
            return response.json()

    async def _call_agent_event(
        *, channel: str, channel_account_id: str, conversation_id: str, approval_id: str, decision: str
    ) -> dict:
        token = issuer.issue(service_name="channels", audience="inumi-agent")
        async with httpx.AsyncClient(
            base_url=settings.agent_base_url,
            transport=agent_transport,
            timeout=60,
        ) as client:
            response = await client.post(
                "/v1/chat/events",
                headers={"X-Service-Token": token},
                json={
                    "channel": channel,
                    "channel_account_id": channel_account_id,
                    "conversation_id": conversation_id,
                    "approval_id": approval_id,
                    "decision": decision,
                },
            )
            response.raise_for_status()
            return response.json()

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # ------------------------------------------------------------------ Slack

    @app.post("/webhooks/slack")
    async def slack_webhook(
        request: Request,
        x_slack_request_timestamp: str | None = Header(default=None),
        x_slack_signature: str | None = Header(default=None),
    ) -> dict:
        raw_body = await request.body()
        try:
            verify_slack_signature(
                signing_secret=settings.slack_signing_secret,
                request_body=raw_body,
                timestamp_header=x_slack_request_timestamp,
                signature_header=x_slack_signature,
            )
        except SlackSignatureError as exc:
            logger.warning("slack_signature_rejected", detail=str(exc))
            raise HTTPException(status_code=401, detail="Invalid Slack signature.") from exc

        payload = json.loads(raw_body or b"{}")

        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}

        if payload.get("type") == "event_callback":
            event = payload.get("event", {})
            if event.get("type") != "message" or event.get("bot_id"):
                return {"ok": True}

            slack_user_id = event["user"]
            identity = await identity_provider.resolve_by_external_account("slack", slack_user_id)
            if identity is None or not identity.is_dba():
                await slack_sender.post_message(
                    event["channel"], "Sorry, this account isn't recognized as a DBA team member.", []
                )
                return {"ok": True}

            conversation_id = f"slack:{event.get('channel')}:{event.get('thread_ts', event.get('ts'))}"
            reply = await _call_agent_chat(
                channel="slack",
                channel_account_id=slack_user_id,
                conversation_id=conversation_id,
                channel_thread_id=event.get("thread_ts", event.get("ts", "")),
                message=event.get("text", ""),
            )

            agent_reply = AgentReply.model_validate(reply)
            await slack_sender.post_message(
                event["channel"], agent_reply.text, render_reply_blocks(agent_reply)
            )
            return {"ok": True}

        return {"ok": True}

    @app.post("/webhooks/slack/interactive")
    async def slack_interactive(request: Request) -> dict:
        form = await request.form()
        raw_payload = form.get("payload", "{}")
        if not isinstance(raw_payload, str):
            raise HTTPException(status_code=400, detail="Malformed interactive payload.")
        payload = json.loads(raw_payload)

        user_id = payload.get("user", {}).get("id", "")
        actions = payload.get("actions", [])
        if not actions:
            return {"ok": True}
        action = actions[0]
        approval_id = action.get("value")
        decision = "approve" if action.get("action_id") == "inumi_approve" else "reject"

        identity = await identity_provider.resolve_by_external_account("slack", user_id)
        if identity is None or not identity.is_dba():
            return {"text": "This account isn't recognized as a DBA team member."}

        channel_id = payload.get("channel", {}).get("id", "")
        conversation_id = f"slack:{channel_id}:{payload.get('container', {}).get('message_ts', '')}"
        reply = await _call_agent_event(
            channel="slack",
            channel_account_id=user_id,
            conversation_id=conversation_id,
            approval_id=approval_id,
            decision=decision,
        )

        agent_reply = AgentReply.model_validate(reply)
        await slack_sender.post_message(channel_id, agent_reply.text, render_reply_blocks(agent_reply))
        return {"ok": True}

    # ------------------------------------------------------------------ Teams

    @app.post("/webhooks/teams")
    async def teams_webhook(request: Request, authorization: str | None = Header(default=None)) -> dict:
        try:
            await teams_verifier.verify(authorization)
        except TeamsAuthError as exc:
            logger.warning("teams_auth_rejected", detail=str(exc))
            raise HTTPException(status_code=401, detail="Invalid Teams bot token.") from exc

        activity = await request.json()
        aad_object_id = activity.get("from", {}).get("aadObjectId") or activity.get("from", {}).get("id", "")

        identity = await identity_provider.resolve_by_external_account("teams", aad_object_id)
        if identity is None or not identity.is_dba():
            return {"type": "message", "text": "Sorry, this account isn't recognized as a DBA team member."}

        conversation_id = f"teams:{activity.get('conversation', {}).get('id', '')}"

        value = activity.get("value") or {}
        if value.get("inumi_action") in ("approve", "reject"):
            reply = await _call_agent_event(
                channel="teams",
                channel_account_id=aad_object_id,
                conversation_id=conversation_id,
                approval_id=value.get("approval_id", ""),
                decision=value["inumi_action"],
            )
        else:
            reply = await _call_agent_chat(
                channel="teams",
                channel_account_id=aad_object_id,
                conversation_id=conversation_id,
                channel_thread_id=activity.get("conversation", {}).get("id", ""),
                message=activity.get("text", ""),
            )

        agent_reply = AgentReply.model_validate(reply)
        card = render_reply_card(agent_reply)
        await teams_sender.send_reply_to_activity(
            activity.get("serviceUrl", ""), activity.get("conversation", {}).get("id", ""), activity.get("id", ""), card
        )
        return {"type": "message", "text": agent_reply.text}

    # ------------------------------------------------------------------ Dev/mock channel (spec §66)

    @app.post("/dev/chat")
    async def dev_chat(body: DevChatRequest) -> dict:
        """Mock channel for local development (spec §66) — still goes
        through real identity resolution + the real Agent/Gateway pipeline;
        only the transport (no real Slack/Teams servers) is mocked."""
        identity = await identity_provider.resolve_by_external_account("dev", body.user)
        if identity is None:
            raise HTTPException(status_code=401, detail="Unknown development user.")
        if not identity.is_dba():
            return {"text": "This account isn't recognized as a DBA team member.", "status": "denied"}

        reply = await _call_agent_chat(
            channel="dev",
            channel_account_id=body.user,
            conversation_id=body.conversation_id,
            channel_thread_id="",
            message=body.message,
        )
        return reply

    @app.post("/dev/chat/events")
    async def dev_chat_event(body: dict) -> dict:
        reply = await _call_agent_event(
            channel="dev",
            channel_account_id=body["user"],
            conversation_id=body.get("conversation_id", "dev-conversation"),
            approval_id=body["approval_id"],
            decision=body["decision"],
        )
        return reply

    return app


app = create_app()
