"""ToolClient (spec §6, §18, §36).

The Agent's *only* route to a tool call is through this client, which talks
exclusively to the Gateway over HTTP with a signed service token. There is
no other network path out of the Agent process toward a database, the
Execution Service, or the Gateway's internals.
"""

from __future__ import annotations

from typing import Any

import httpx

from inumi.common.models.tool import ToolCallRequest, ToolCallResponse, ToolDefinition
from inumi.common.service_auth import ServiceTokenIssuer


class ToolClient:
    def __init__(
        self,
        base_url: str,
        issuer: ServiceTokenIssuer,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url
        self._issuer = issuer
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        token = self._issuer.issue(service_name="agent", audience="inumi-gateway")
        return {"X-Service-Token": token}

    async def available_tools(
        self, channel: str, channel_account_id: str
    ) -> list[ToolDefinition]:
        async with httpx.AsyncClient(base_url=self._base_url, transport=self._transport) as client:
            response = await client.get(
                "/v1/tools",
                params={"channel": channel, "channel_account_id": channel_account_id},
                headers=self._headers(),
            )
            response.raise_for_status()
            return [ToolDefinition.model_validate(t) for t in response.json()]

    async def submit(self, request: ToolCallRequest) -> ToolCallResponse:
        async with httpx.AsyncClient(base_url=self._base_url, transport=self._transport, timeout=60) as client:
            response = await client.post(
                "/v1/tool-calls", json=request.model_dump(mode="json"), headers=self._headers()
            )
            response.raise_for_status()
            return ToolCallResponse.model_validate(response.json())

    async def approve(self, approval_id: str, channel: str, channel_account_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self._base_url, transport=self._transport) as client:
            response = await client.post(
                f"/v1/approvals/{approval_id}/approve",
                json={"channel": channel, "channel_account_id": channel_account_id},
                headers=self._headers(),
            )
            if response.status_code >= 400:
                return {"status": "ERROR", "detail": response.json().get("detail", "error")}
            return response.json()

    async def reject(self, approval_id: str, channel: str, channel_account_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self._base_url, transport=self._transport) as client:
            response = await client.post(
                f"/v1/approvals/{approval_id}/reject",
                json={"channel": channel, "channel_account_id": channel_account_id},
                headers=self._headers(),
            )
            if response.status_code >= 400:
                return {"status": "ERROR", "detail": response.json().get("detail", "error")}
            return response.json()
