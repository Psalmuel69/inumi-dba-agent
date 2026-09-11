"""Discovery orchestration (Gateway side).

Asks the Execution Service to crawl each registered server and stores the
resulting catalog. Triggered on Gateway startup (best-effort background),
by `POST /v1/catalog/refresh` (DBA_MANAGER), and lazily the first time a
server with no catalog is targeted by a read-only operation.
"""

from __future__ import annotations

import datetime as dt

from inumi.common.config import Settings
from inumi.common.models.catalog import ServerCatalog
from inumi.common.models.execution import DiscoveryRequest
from inumi.common.observability import get_logger
from inumi.gateway.domain.catalog import CatalogStore
from inumi.gateway.domain.servers import ServerRegistry
from inumi.gateway.infrastructure.execution_client import ExecutionClient

logger = get_logger(__name__)


class DiscoveryOrchestrator:
    def __init__(
        self,
        *,
        registry: ServerRegistry,
        catalog_store: CatalogStore,
        execution_client: ExecutionClient,
        settings: Settings,
    ):
        self._registry = registry
        self._store = catalog_store
        self._execution = execution_client
        self._settings = settings

    async def refresh_server(self, server_id: str) -> ServerCatalog:
        server = self._registry.by_id(server_id)
        if server is None:
            raise LookupError(f"No registered server '{server_id}'.")
        catalog = await self._execution.discover(
            DiscoveryRequest(
                server_id=server.id,
                platform=server.platform,
                max_objects_per_database=self._settings.discovery_max_objects_per_database,
            )
        )
        await self._store.put(catalog)
        logger.info(
            "catalog_refreshed",
            server_id=server_id,
            databases=len(catalog.databases),
            warnings=len(catalog.warnings),
        )
        return catalog

    async def refresh_all(self) -> dict[str, str]:
        results: dict[str, str] = {}
        for server in self._registry.all():
            if server.status != "active":
                continue
            try:
                cat = await self.refresh_server(server.id)
                results[server.id] = f"ok ({len(cat.databases)} databases)"
            except Exception as exc:  # noqa: BLE001 — one bad server never blocks the rest
                results[server.id] = f"failed: {exc}"
                logger.warning("catalog_refresh_failed", server_id=server.id, error=str(exc))
        return results

    async def ensure_fresh(self, server_id: str) -> None:
        """Lazily (re)discover if the catalog is missing or stale."""
        existing = await self._store.get(server_id)
        if existing is not None and existing.discovered_at is not None:
            age = dt.datetime.now(dt.UTC) - existing.discovered_at.replace(
                tzinfo=existing.discovered_at.tzinfo or dt.UTC
            )
            if age < dt.timedelta(minutes=self._settings.discovery_refresh_minutes):
                return
        try:
            await self.refresh_server(server_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lazy_discovery_failed", server_id=server_id, error=str(exc))
