"""Gateway FastAPI app (spec §57).

This process is the DBA Control Gateway described throughout the spec: the
only thing between the Agent's tool requests and the Execution Service.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from inumi.common.config import Settings, get_settings
from inumi.common.observability import configure_logging, get_logger
from inumi.gateway.api.routers import approvals, audit, investigations, tool_calls, tools
from inumi.gateway.api.state import GatewayState

logger = get_logger(__name__)


def create_app(settings: Settings | None = None, *, execution_transport=None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging("gateway", settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = GatewayState.build(settings, execution_transport=execution_transport)
        if settings.control_db_url.startswith("sqlite"):
            # Local dev/test convenience only — real deployments apply the
            # Alembic migrations in migrations/versions instead.
            await state.db.create_all()
        app.state.gateway = state
        yield
        await state.db.dispose()

    app = FastAPI(title="Inumi DBA Control Gateway", version="0.1.0", lifespan=lifespan)

    app.include_router(tool_calls.router)
    app.include_router(tools.router)
    app.include_router(approvals.router)
    app.include_router(investigations.router)
    app.include_router(audit.router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict:
        return {"status": "ready"}

    return app


app = create_app()
