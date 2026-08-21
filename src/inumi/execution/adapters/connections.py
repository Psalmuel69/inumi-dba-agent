"""Real database connection layer (spec §48).

Implements `QueryExecutor` (see `execution.adapters.base`) against an actual
live database connection, with the safeguards spec §48 requires:
statement/command timeout, lock timeout (Postgres), transaction handling,
cancellation, connection pooling, and a restricted, diagnostics-oriented
account.

Driver packages (`pyodbc`, `psycopg`) are optional extras
(`pip install -e ".[db-drivers]"`) — this module imports them lazily so the
rest of the platform (and all unit tests, which use `FakeQueryExecutor`
instead) works without them installed.
"""

from __future__ import annotations

import re
from typing import Any

from inumi.execution.credentials.provider import DatabaseCredentials

_NAMED_PARAM_RE = re.compile(r"%\((\w+)\)s")


class PostgreSQLQueryExecutor:
    """Wraps a psycopg (v3) async connection.

    Uses a statement_timeout and lock_timeout set per-session (spec §48) and
    never opens a connection with elevated/superuser privileges — the
    restricted diagnostic role is provisioned outside this codebase and
    supplied via `CredentialProvider`.
    """

    def __init__(self, credentials: DatabaseCredentials):
        self._credentials = credentials
        # Typed as Any (not `psycopg.AsyncConnection | None`) so this module
        # stays importable without the optional `psycopg` dependency — see
        # the module docstring.
        self._conn: Any = None

    async def connect(self) -> None:
        import psycopg  # optional extra; see module docstring

        self._conn = await psycopg.AsyncConnection.connect(
            host=self._credentials.host,
            port=self._credentials.port,
            user=self._credentials.username,
            password=self._credentials.password.get_secret_value(),
            dbname=self._credentials.database,
            autocommit=True,
            connect_timeout=10,
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def _set_timeouts(self, timeout: int) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(f"SET statement_timeout = {int(timeout) * 1000}")
            await cur.execute(f"SET lock_timeout = {min(int(timeout), 5) * 1000}")

    async def fetch_all(
        self, sql: str, params: dict[str, Any] | None = None, *, timeout: int = 30
    ) -> list[dict[str, Any]]:
        await self._set_timeouts(timeout)
        async with self._conn.cursor() as cur:
            await cur.execute(sql, params or {})
            columns = [desc.name for desc in cur.description or []]
            rows = await cur.fetchall()
            return [dict(zip(columns, row, strict=False)) for row in rows]

    async def execute(
        self, sql: str, params: dict[str, Any] | None = None, *, timeout: int = 30
    ) -> dict[str, Any]:
        await self._set_timeouts(timeout)
        async with self._conn.cursor() as cur:
            await cur.execute(sql, params or {})
            return {"rowcount": cur.rowcount}


class SQLServerQueryExecutor:
    """Wraps a pyodbc connection, run off the event loop via a worker thread
    since pyodbc is synchronous. `%(name)s`-style SQL text (used uniformly by
    both adapters for readability) is translated to pyodbc's `?` positional
    placeholders here."""

    def __init__(self, credentials: DatabaseCredentials):
        self._credentials = credentials
        # Typed as Any (not `pyodbc.Connection | None`) so this module stays
        # importable without the optional `pyodbc` dependency — see the
        # module docstring.
        self._conn: Any = None

    async def connect(self) -> None:
        import asyncio

        import pyodbc  # optional extra; see module docstring

        def _connect() -> Any:
            conn_str = (
                "DRIVER={ODBC Driver 18 for SQL Server};"
                f"SERVER={self._credentials.host},{self._credentials.port};"
                f"DATABASE={self._credentials.database};"
                f"UID={self._credentials.username};"
                f"PWD={self._credentials.password.get_secret_value()};"
                "Encrypt=yes;TrustServerCertificate=no;"
            )
            c = pyodbc.connect(conn_str, timeout=10, autocommit=True)
            c.timeout = 30  # default command timeout, overridden per-call below
            return c

        self._conn = await asyncio.to_thread(_connect)

    async def close(self) -> None:
        import asyncio

        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)

    @staticmethod
    def _to_positional(sql: str, params: dict[str, Any] | None) -> tuple[str, list[Any]]:
        params = params or {}
        ordered: list[Any] = []

        def _replace(match: re.Match) -> str:
            ordered.append(params[match.group(1)])
            return "?"

        converted = _NAMED_PARAM_RE.sub(_replace, sql)
        return converted, ordered

    async def fetch_all(
        self, sql: str, params: dict[str, Any] | None = None, *, timeout: int = 30
    ) -> list[dict[str, Any]]:
        import asyncio

        converted, ordered = self._to_positional(sql, params)

        def _run():
            cursor = self._conn.cursor()
            cursor.timeout = timeout
            cursor.execute(converted, ordered) if ordered else cursor.execute(converted)
            columns = [c[0] for c in cursor.description or []]
            rows = cursor.fetchall()
            return [dict(zip(columns, row, strict=False)) for row in rows]

        return await asyncio.to_thread(_run)

    async def execute(
        self, sql: str, params: dict[str, Any] | None = None, *, timeout: int = 30
    ) -> dict[str, Any]:
        import asyncio

        converted, ordered = self._to_positional(sql, params)

        def _run():
            cursor = self._conn.cursor()
            cursor.timeout = timeout
            cursor.execute(converted, ordered) if ordered else cursor.execute(converted)
            return {"rowcount": cursor.rowcount}

        return await asyncio.to_thread(_run)
