"""Discovery — reading a server's DBA-relevant *metadata* into the catalog.

Runs in the Execution Service (the only component with database access). A
`ServerDiscoverer` enumerates, for one registered server:

  - engine version / edition and instance-level properties/settings
  - every non-system database: state, size, options
  - each database's schemas and objects (tables / views / indexes / procs)
    with catalog row estimates and sizes — **never row data**
  - available and installed extensions

The diagnostic login only needs read access to catalog / DMV / stats views
(VIEW SERVER STATE + VIEW DEFINITION on SQL Server; the `pg_monitor` role on
PostgreSQL). It should NOT have SELECT on user tables — Inumi never reads
table contents.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod

from inumi.common.models.catalog import ServerCatalog
from inumi.execution.adapters.base import QueryExecutor
from inumi.execution.credentials.provider import DatabaseCredentials


class ServerDiscoverer(ABC):
    """One instance per discovery run, bound to a server's credentials."""

    def __init__(self, credentials: DatabaseCredentials, *, max_objects_per_database: int = 5000):
        self._credentials = credentials
        self._max_objects = max_objects_per_database

    @abstractmethod
    async def discover(self, server_id: str) -> ServerCatalog:
        """Produce the full catalog for `server_id`. Best-effort: a database
        the login can't see is skipped, not fatal."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _fetch(executor: QueryExecutor, sql: str, params: dict | None = None) -> list[dict]:
    return await executor.fetch_all(sql, params or {}, timeout=30)
