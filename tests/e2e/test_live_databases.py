"""Opt-in end-to-end tests against real database engines (spec §48).

Skipped unless a matching database in config/dev_credentials.yaml is
actually reachable, so `pytest` still passes on a machine with no
databases. Bring the sample databases up with:

    docker compose up -d postgres-sample
    #  (SQL Server, opt-in / heavy)
    docker compose --profile mssql up -d mssql-sample

These exercise the *real* SQLServerAdapter / PostgreSQLAdapter query text
against a live connection — the layer that `FakeQueryExecutor` stands in
for elsewhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

from inumi.execution.adapters.postgresql import PostgreSQLAdapter
from inumi.execution.adapters.sqlserver import SQLServerAdapter
from inumi.execution.credentials.provider import LocalDevCredentialProvider

_OPT_IN = os.environ.get("RUN_LIVE_DB_TESTS") == "1"
_CREDS_PATH = "config/dev_credentials.yaml"


async def _pg_executor_or_skip(database_id: str):
    from inumi.execution.adapters.connections import PostgreSQLQueryExecutor

    provider = LocalDevCredentialProvider(_CREDS_PATH)
    creds = await provider.get_credentials(database_id)
    executor = PostgreSQLQueryExecutor(creds)
    try:
        await asyncio.wait_for(executor.connect(), timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"postgres '{database_id}' not reachable: {exc}")
    return executor


async def _mssql_executor_or_skip(database_id: str):
    from inumi.execution.adapters.connections import SQLServerQueryExecutor

    provider = LocalDevCredentialProvider(_CREDS_PATH)
    creds = await provider.get_credentials(database_id)
    executor = SQLServerQueryExecutor(creds)
    try:
        await asyncio.wait_for(executor.connect(), timeout=5)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"sql server '{database_id}' not reachable: {exc}")
    return executor


pytestmark = pytest.mark.skipif(
    not _OPT_IN or not os.path.exists(_CREDS_PATH),
    reason="Live DB tests are opt-in — set RUN_LIVE_DB_TESTS=1 and provide config/dev_credentials.yaml.",
)


async def test_live_postgres_health_and_blocking_queries_run():
    executor = await _pg_executor_or_skip("postgres-prod-cluster-01")
    adapter = PostgreSQLAdapter(executor, "sample_analytics")
    try:
        health = await adapter.health()
        assert isinstance(health, list) and health
        assert "active_connections" in health[0]

        blocking = await adapter.blocking()
        assert isinstance(blocking, list)  # usually empty on an idle db — that's fine

        waits = await adapter.waits()
        assert isinstance(waits, list)
    finally:
        with contextlib.suppress(Exception):
            await executor.close()


async def test_live_sqlserver_health_and_sessions_queries_run():
    executor = await _mssql_executor_or_skip("corebanking-dev-01")
    adapter = SQLServerAdapter(executor, "AdventureWorks2019")
    try:
        version = await adapter.version()
        assert version and "version" in version[0]

        sessions = await adapter.sessions()
        assert isinstance(sessions, list)
    finally:
        with contextlib.suppress(Exception):
            await executor.close()
