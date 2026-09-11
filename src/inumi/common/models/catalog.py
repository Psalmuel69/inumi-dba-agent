"""Discovered catalog models — what Inumi has learned about each registered
server.

Shared between the Execution Service (discovery crawler, which produces
these) and the Gateway (which caches and serves them). Everything here is
DBA *metadata*: database names/states/sizes, schema and object names, index
stats, available extensions, server/instance properties. **Never table or
view row data.**

Object names and comments coming from a database are untrusted strings —
treated as data, never instructions (spec §23).
"""

from __future__ import annotations

import datetime as dt

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
    # Non-fatal problems hit during the crawl (a db the login couldn't read).
    warnings: list[str] = Field(default_factory=list)

    def database(self, name: str) -> DiscoveredDatabase | None:
        lowered = name.strip().lower()
        return next((d for d in self.databases if d.name.lower() == lowered), None)

    def database_names(self) -> list[str]:
        return [d.name for d in self.databases]
