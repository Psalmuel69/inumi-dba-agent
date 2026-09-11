"""Catalog + discovery endpoints through the Gateway HTTP API (spec §11, §39).

The Execution app is built with the canned adapter factory, so `/v1/discover`
returns an empty `ServerCatalog` deterministically — enough to exercise the
Gateway's own routing, persistence and authorization.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.stack import build_stack


async def test_list_servers_reflects_the_registry_before_any_discovery():
    stack = await build_stack()
    with TestClient(stack.gateway_app) as client:
        resp = client.get(
            "/v1/catalog/servers",
            headers={"X-Service-Token": stack.agent_service_token()},
        )
        assert resp.status_code == 200
        servers = {s["id"]: s for s in resp.json()}
        assert "sqlserver-dev-01" in servers
        assert servers["sqlserver-dev-01"]["catalog"] is None  # not yet crawled
        assert servers["corebanking-sqlserver-prod"]["environment"] == "production"


async def test_refresh_requires_dba_manager():
    stack = await build_stack()
    with TestClient(stack.gateway_app) as client:
        token = stack.agent_service_token()

        denied = client.post(
            "/v1/catalog/refresh",
            headers={"X-Service-Token": token},
            json={"channel": "slack", "channel_account_id": "U_MOCK_L2"},
        )
        assert denied.status_code == 403

        ok = client.post(
            "/v1/catalog/refresh",
            headers={"X-Service-Token": token},
            json={"channel": "slack", "channel_account_id": "U_MOCK_MGR"},
        )
        assert ok.status_code == 200
        assert "sqlserver-dev-01" in ok.json()


async def test_refresh_one_then_read_back_the_persisted_catalog():
    stack = await build_stack()
    with TestClient(stack.gateway_app) as client:
        token = stack.agent_service_token()

        refreshed = client.post(
            "/v1/catalog/refresh/sqlserver-dev-01",
            headers={"X-Service-Token": token},
            json={"channel": "slack", "channel_account_id": "U_MOCK_MGR"},
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["server_id"] == "sqlserver-dev-01"

        got = client.get(
            "/v1/catalog/servers/sqlserver-dev-01",
            headers={"X-Service-Token": token},
        )
        assert got.status_code == 200
        assert got.json()["catalog"] is not None
        assert got.json()["catalog"]["server_id"] == "sqlserver-dev-01"


async def test_refresh_unknown_server_is_404():
    stack = await build_stack()
    with TestClient(stack.gateway_app) as client:
        resp = client.post(
            "/v1/catalog/refresh/does-not-exist",
            headers={"X-Service-Token": stack.agent_service_token()},
            json={"channel": "slack", "channel_account_id": "U_MOCK_MGR"},
        )
        assert resp.status_code == 404


async def test_catalog_endpoints_reject_a_forged_service_token():
    stack = await build_stack()
    with TestClient(stack.gateway_app) as client:
        resp = client.get(
            "/v1/catalog/servers",
            headers={"X-Service-Token": stack.forged_token()},
        )
        assert resp.status_code == 401
