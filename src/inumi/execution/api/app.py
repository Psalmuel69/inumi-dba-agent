"""Execution Service FastAPI app (spec §18, §31).

Exposes exactly one privileged endpoint, `/v1/execute`, and it is only ever
reachable with a valid, audience-scoped service token minted by the Gateway
(`inumi.common.service_auth`) — there is no route, header, or flag that lets
the Agent or a channel adapter call this service directly.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException

from inumi.common.config import Settings, get_settings
from inumi.common.models.execution import ExecutionRequest, ExecutionResult
from inumi.common.observability import configure_logging, get_logger
from inumi.common.service_auth import ServiceTokenVerifier
from inumi.execution.credentials.provider import build_credential_provider
from inumi.execution.service import ExecutionService

logger = get_logger(__name__)


def create_app(settings: Settings | None = None, *, adapter_factory=None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_for_production()
    configure_logging("execution-service", settings.log_level)

    app = FastAPI(title="Inumi Execution Service", version="0.1.0")
    verifier = ServiceTokenVerifier(settings.service_jwt_secret, settings.service_jwt_issuer)
    credential_provider = build_credential_provider(settings)
    service = ExecutionService(settings, credential_provider, adapter_factory=adapter_factory)

    async def require_gateway_service_token(
        x_service_token: str | None = Header(default=None),
    ) -> None:
        if not x_service_token:
            raise HTTPException(status_code=401, detail="Missing service token.")
        try:
            verifier.verify(
                x_service_token, expected_audience=settings.execution_service_token_audience
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=401, detail="Invalid service token.") from exc

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict:
        return {"status": "ready"}

    @app.post("/v1/execute", response_model=ExecutionResult, dependencies=[Depends(require_gateway_service_token)])
    async def execute(request: ExecutionRequest) -> ExecutionResult:
        logger.info(
            "execution_request_received",
            tool_id=request.tool_id,
            server_id=request.server_id,
            execution_id=request.execution_id,
        )
        result = await service.execute(request)
        logger.info(
            "execution_request_completed",
            execution_id=request.execution_id,
            success=result.success,
            error_code=result.error_code,
        )
        return result

    return app


app = create_app()
