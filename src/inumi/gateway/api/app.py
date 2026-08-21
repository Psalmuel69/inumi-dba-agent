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

    # Built eagerly (not inside `lifespan`) so tests driving this app via
    # `httpx.ASGITransport` — which does not emit ASGI lifespan events —
    # can reach `app.state.gateway` and call `state.db.create_all()`
    # themselves without needing a running server.
    state = GatewayState.build(settings, execution_transport=execution_transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.control_db_url.startswith("sqlite"):
            # Local dev/test convenience only — real deployments apply the
            # Alembic migrations in migrations/versions instead.
            await state.db.create_all()
        yield
        await state.db.dispose()

    app = FastAPI(title="Inumi DBA Control Gateway", version="0.1.0", lifespan=lifespan)
    app.state.gateway = state

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
