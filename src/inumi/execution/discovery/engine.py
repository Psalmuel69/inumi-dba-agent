"""Discovery dispatcher — picks the platform discoverer and runs it."""

from __future__ import annotations

from inumi.common.models.catalog import ServerCatalog
from inumi.common.models.target import Platform
from inumi.execution.credentials.provider import DatabaseCredentials
from inumi.execution.discovery.base import ServerDiscoverer
from inumi.execution.discovery.postgresql import PostgreSQLDiscoverer
from inumi.execution.discovery.sqlserver import SQLServerDiscoverer

_DISCOVERERS: dict[Platform, type[ServerDiscoverer]] = {
    Platform.SQLSERVER: SQLServerDiscoverer,
    Platform.POSTGRESQL: PostgreSQLDiscoverer,
}


def _discoverer_for(platform: Platform) -> type[ServerDiscoverer]:
    cls = _DISCOVERERS.get(platform)
    if cls is None:
        raise NotImplementedError(
            f"No discoverer for platform '{platform.value}'. Adding an engine means "
            "implementing ServerDiscoverer and registering it here."
        )
    return cls


async def run_discovery(
    *,
    server_id: str,
    platform: Platform,
    credentials: DatabaseCredentials,
    max_objects_per_database: int = 5000,
) -> ServerCatalog:
    discoverer = _discoverer_for(platform)(
        credentials, max_objects_per_database=max_objects_per_database
    )
    return await discoverer.discover(server_id)
