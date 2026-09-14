"""Channels service tests — the full stack wired end to end.

channels -> agent -> gateway -> execution, all via in-process ASGI
transports, verifying the whole chain described in spec §4/§66 works,
including identity verification, signature verification, and the dev mock
channel.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inumi.agent.api.app import create_app as create_agent_app
from inumi.channels.api.app import _slack_conversation_id
from inumi.channels.api.app import create_app as create_channels_app
from inumi.common.config import Settings
from inumi.execution.api.app import create_app as create_execution_app
from inumi.gateway.api.app import create_app as create_gateway_app
from tests.canned_adapter import canned_adapter_factory


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        control_db_url="sqlite+aiosqlite:///:memory:",
        service_jwt_secret="test-secret",
        service_jwt_issuer="inumi-internal",
        llm_provider="mock",
        slack_signing_secret="test-slack-signing-secret",
        teams_app_password="dev-teams-shared-token",
        **overrides,
    )


def _build_full_stack(settings: Settings):
    execution_app = create_execution_app(settings, adapter_factory=canned_adapter_factory)
    execution_transport = httpx.ASGITransport(app=execution_app)
    gateway_app = create_gateway_app(settings, execution_transport=execution_transport)
    # TestClient wraps the *channels* app's lifespan only; the gateway app's
    # in-memory sqlite schema is created explicitly here instead of relying
    # on its own (unentered) lifespan.
    asyncio.run(gateway_app.state.gateway.db.create_all())
    gateway_transport = httpx.ASGITransport(app=gateway_app)
    agent_app = create_agent_app(settings, gateway_transport=gateway_transport)
    agent_transport = httpx.ASGITransport(app=agent_app)
    channels_app = create_channels_app(settings, agent_transport=agent_transport)
    return gateway_app, channels_app


def _sign_slack(secret: str, body: bytes, timestamp: str) -> str:
    base = f"v0:{timestamp}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_slack_bad_signature_is_rejected():
    settings = _settings()
    _gw, channels_app = _build_full_stack(settings)
    with TestClient(channels_app) as client:
        response = client.post(
            "/webhooks/slack",
            content=b'{"type": "url_verification", "challenge": "abc"}',
            headers={
                "X-Slack-Request-Timestamp": str(int(time.time())),
                "X-Slack-Signature": "v0=deadbeef",
            },
        )
        assert response.status_code == 401


def test_slack_url_verification_challenge():
    settings = _settings()
    _gw, channels_app = _build_full_stack(settings)
    body = b'{"type": "url_verification", "challenge": "abc123"}'
    ts = str(int(time.time()))
    sig = _sign_slack(settings.slack_signing_secret, body, ts)
    with TestClient(channels_app) as client:
        response = client.post(
            "/webhooks/slack",
            content=body,
            headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig},
        )
        assert response.status_code == 200
        assert response.json() == {"challenge": "abc123"}


def test_slack_message_from_verified_dba_is_processed():
    settings = _settings()
    _gw, channels_app = _build_full_stack(settings)
    import json

    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "message",
                "user": "U_MOCK_L2",
                "text": "hello",
                "channel": "C123",
                "ts": "111.222",
            },
        }
    ).encode()
    ts = str(int(time.time()))
    sig = _sign_slack(settings.slack_signing_secret, body, ts)
    with TestClient(channels_app) as client:
        response = client.post(
            "/webhooks/slack",
            content=body,
            headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}


def test_a_retried_slack_event_is_deduplicated_not_reprocessed(capsys):
    """Reproduces a live finding: Slack retries a webhook delivery it
    hasn't gotten a fast ack for (its own ~3s timeout), reusing the same
    event_id — and this handler awaits the full Agent round-trip before
    ever returning, which a real multi-turn investigation can easily
    exceed. A real DBA's single Slack message produced two different,
    garbled replies as a result. The fix must make a retried delivery
    (same event_id) a no-op instead of a second, independent run against
    the same shared conversation."""
    settings = _settings()
    _gw, channels_app = _build_full_stack(settings)
    import json

    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev_DEDUP_TEST_1",
            "event": {
                "type": "message",
                "user": "U_MOCK_L2",
                "text": "hello",
                "channel": "C123",
                "ts": "111.222",
            },
        }
    ).encode()
    ts = str(int(time.time()))
    sig = _sign_slack(settings.slack_signing_secret, body, ts)
    headers = {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}
    with TestClient(channels_app) as client:
        first = client.post("/webhooks/slack", content=body, headers=headers)
        assert first.status_code == 200
        # Slack's own retry: identical payload, identical event_id.
        second = client.post("/webhooks/slack", content=body, headers=headers)
        assert second.status_code == 200

    assert "slack_event_retry_deduplicated" in capsys.readouterr().out


def test_teams_webhook_requires_valid_dev_token():
    settings = _settings()
    _gw, channels_app = _build_full_stack(settings)
    with TestClient(channels_app) as client:
        response = client.post(
            "/webhooks/teams",
            json={"from": {"aadObjectId": "aad-mock-l2"}, "text": "hi", "conversation": {"id": "c1"}},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401


def test_teams_webhook_with_valid_dev_token_and_verified_dba():
    settings = _settings()
    _gw, channels_app = _build_full_stack(settings)
    with TestClient(channels_app) as client:
        response = client.post(
            "/webhooks/teams",
            json={"from": {"aadObjectId": "aad-mock-l2"}, "text": "hi", "conversation": {"id": "c1"}},
            headers={"Authorization": "Bearer dev-teams-shared-token"},
        )
        assert response.status_code == 200


def test_dev_chat_full_round_trip_investigation_and_approval():
    """Spec §66's exact worked example, run through the whole stack."""
    settings = _settings()
    gateway_app, channels_app = _build_full_stack(settings)
    with TestClient(channels_app) as client:
        response = client.post(
            "/dev/chat",
            json={
                "user": "dba_l2@example.com",
                "message": "Check blocking on CoreBanking production.",
                "conversation_id": "dev_conv_1",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approval_required"
        approval_id = body["approval_card"]["approval_id"]

        approve_response = client.post(
            "/dev/chat/events",
            json={
                "user": "dba_l2@example.com",
                "conversation_id": "dev_conv_1",
                "approval_id": approval_id,
                "decision": "approve",
            },
        )
        assert approve_response.status_code == 200
        assert "Action approved" in approve_response.json()["text"]


def test_dev_chat_unknown_user_is_rejected():
    settings = _settings()
    _gw, channels_app = _build_full_stack(settings)
    with TestClient(channels_app) as client:
        response = client.post(
            "/dev/chat", json={"user": "nobody@example.com", "message": "hi"}
        )
        assert response.status_code == 401


def test_dev_chat_non_dba_user_is_denied():
    settings = _settings()
    _gw, channels_app = _build_full_stack(settings)
    with TestClient(channels_app) as client:
        response = client.post(
            "/dev/chat", json={"user": "notadba@example.com", "message": "hi"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "denied"


def test_slack_conversation_id_for_a_threaded_reply_is_unchanged():
    """A threaded reply's conversation is still scoped by thread_ts alone
    — unchanged, and deliberately shared by whoever else replies in the
    same thread, since a Slack thread is inherently one collaborative
    investigation."""
    assert (
        _slack_conversation_id("C123", "U_A", "100.001")
        == _slack_conversation_id("C123", "U_B", "100.001")
        == "slack:C123:100.001"
    )


def test_slack_conversation_id_for_plain_messages_from_the_same_dba_is_stable():
    """Live-reproduced regression: an ordinary, non-threaded message used
    to fall back to its own `ts` — unique to that one message — so two
    consecutive plain messages from the same DBA in the same channel
    (exactly how a DBA naturally follows up when the bot "responds to
    plain messages too", with no @-mention and no thread reply) got two
    different conversation_ids, and therefore two entirely separate,
    empty `ConversationState`s at the Agent. Scoped to `(channel, user)`
    now, so consecutive plain messages share one conversation."""
    first = _slack_conversation_id("C123", "U_MOCK_L2", "")
    second = _slack_conversation_id("C123", "U_MOCK_L2", "")
    assert first == second


def test_slack_conversation_id_for_plain_messages_from_different_dbas_never_crosses():
    """The fix must not go too far the other way: two different DBAs
    typing plain (non-threaded) messages in the same shared channel are
    still independent conversations, never merged into one."""
    assert _slack_conversation_id("C123", "U_MOCK_L2", "") != _slack_conversation_id(
        "C123", "U_OTHER", ""
    )


def test_three_plain_non_threaded_slack_messages_from_the_same_dba_share_one_conversation():
    """The exact live reproduction, end to end through the real
    `/webhooks/slack` handler (signature verification, identity
    resolution, the whole path) but with a stub `/v1/chat` standing in for
    the Agent — this isolates the channels-layer bug precisely, without
    depending on any LLM behavior. All three turns of the real, live
    sequence, in order, none of them a threaded reply and none but the
    first naming any entity at all — exactly as this channel invites ("no
    @-mention needed... responds to plain messages too"):

    1. "check backup health on postgres-local" (names the server; the
       investigation that follows concludes normally).
    2. "and what about replication?" — zero named entities; got the wrong
       "Which environment should I investigate?" live.
    3. "development" — a bare answer to exactly that clarification,
       matching `orchestrator._ENVIRONMENT_ANSWER_RE`'s deterministic
       resume path (see tests/unit/test_environment_clarification.py);
       got the generic "tell me more" chitchat fallback live instead of
       resuming.

    All three must reach the Agent as the SAME conversation_id. Before the
    fix, every one of these (having no `thread_ts` of its own) fell back
    to its own unique `ts` — three different, brand-new, empty
    `ConversationState`s in a row. The Agent's own orchestrator logic is
    NOT the bug (verified separately: tests/unit/test_environment_
    clarification.py's test_a_later_fresh_investigation_never_reasks_for_
    an_already_known_environment exercises the same zero-entity follow-up
    shape, and test_a_bare_environment_answer_continues_without_calling_
    extract_intent exercises the same bare-answer resume, both passing —
    only reachable at all when conversation_id is actually held constant,
    which is exactly what a real, non-threaded Slack exchange failed to
    do)."""
    settings = _settings()
    seen_conversation_ids: list[str] = []

    stub_agent = FastAPI()

    @stub_agent.post("/v1/chat")
    async def _chat(body: dict) -> dict:
        seen_conversation_ids.append(body["conversation_id"])
        return {"text": "ok"}

    channels_app = create_channels_app(
        settings, agent_transport=httpx.ASGITransport(app=stub_agent)
    )

    def _post_plain_message(text: str, ts: str) -> None:
        body = json.dumps(
            {
                "type": "event_callback",
                "event": {
                    "type": "message",
                    "user": "U_MOCK_L2",
                    "text": text,
                    "channel": "C123",
                    "ts": ts,
                    # Deliberately no "thread_ts" — an ordinary channel
                    # message, never posted as a threaded reply, exactly
                    # like the live reproduction.
                },
            }
        ).encode()
        request_ts = str(int(time.time()))
        sig = _sign_slack(settings.slack_signing_secret, body, request_ts)
        response = client.post(
            "/webhooks/slack",
            content=body,
            headers={"X-Slack-Request-Timestamp": request_ts, "X-Slack-Signature": sig},
        )
        assert response.status_code == 200

    with TestClient(channels_app) as client:
        _post_plain_message("check backup health on postgres-local", "100.001")
        _post_plain_message("and what about replication?", "100.002")
        _post_plain_message("development", "100.003")

    assert len(seen_conversation_ids) == 3
    assert seen_conversation_ids[0] == seen_conversation_ids[1] == seen_conversation_ids[2]
