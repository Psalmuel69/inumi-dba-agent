"""Discovered catalog — what Inumi has learned about each registered server.

Populated by the discovery crawler (`inumi.execution.discovery`, run through
the Execution Service — the only component with database access) and cached
in the control-plane database. Everything here is DBA *metadata*: database
names/states/sizes, schema and object names, index stats, available
extensions, server/instance properties. **Never table or view row data.**

Object names and comments coming from a database are untrusted strings —
treated as data, never instructions (spec §23).
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict, Field


class DiscoveredObject(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_name: str
    name: str
    kind: str  # table | view | index | procedure | function | sequence
    row_estimate: int | None = None
    size_bytes: int | None = None
    properties: dict = Field(default_factory=dict)


class DiscoveredExtension(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    installed_version: str | None = None
    default_version: str | None = None
    available: bool = True


class DiscoveredDatabase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    state: str = "unknown"
    size_bytes: int | None = None
    options: dict = Field(default_factory=dict)
    objects: list[DiscoveredObject] = Field(default_factory=list)
    extensions: list[DiscoveredExtension] = Field(default_factory=list)


class ServerCatalog(BaseModel):
    model_config = ConfigDict(extra="ignore")

    server_id: str
    discovered_at: dt.datetime | None = None
    engine_version: str = ""
    engine_edition: str = ""
    instance_properties: dict = Field(default_factory=dict)
    databases: list[DiscoveredDatabase] = Field(default_factory=list)

    def database(self, name: str) -> DiscoveredDatabase | None:
        lowered = name.strip().lower()
        return next((d for d in self.databases if d.name.lower() == lowered), None)

    def database_names(self) -> list[str]:
        return [d.name for d in self.databases]


class CatalogStore(ABC):
    """Read/write access to the discovered catalog. The Gateway reads it for
    target validation and to answer `/servers` / `/catalog`; the Execution
    Service (via the Gateway) writes it after a discovery run."""

    @abstractmethod
    async def get(self, server_id: str) -> ServerCatalog | None: ...

    @abstractmethod
    async def put(self, catalog: ServerCatalog) -> None: ...

    @abstractmethod
    async def all(self) -> list[ServerCatalog]: ...


class InMemoryCatalogStore(CatalogStore):
    """Process-local. Fine for a single Gateway instance; a DB-backed store
    (`inumi.gateway.infrastructure.catalog_store`) is used when the control
    DB is available so the catalog survives a restart and is shared across
    replicas."""

    def __init__(self) -> None:
        self._by_server: dict[str, ServerCatalog] = {}

    async def get(self, server_id: str) -> ServerCatalog | None:
        return self._by_server.get(server_id)

    async def put(self, catalog: ServerCatalog) -> None:
        self._by_server[catalog.server_id] = catalog

    async def all(self) -> list[ServerCatalog]:
        return list(self._by_server.values())
