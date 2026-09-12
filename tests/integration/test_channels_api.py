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
import time

import httpx
from fastapi.testclient import TestClient

from inumi.agent.api.app import create_app as create_agent_app
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
