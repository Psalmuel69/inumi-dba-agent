from __future__ import annotations

import pytest

from inumi.execution.adapters.postgresql import PostgreSQLAdapter
from inumi.execution.adapters.sqlserver import SQLServerAdapter
from tests.fakes import FakeQueryExecutor


@pytest.mark.asyncio
async def test_postgres_blocking_uses_pg_locks_and_pg_stat_activity():
    executor = FakeQueryExecutor(
        canned_rows=[{"blocked_session_id": 100, "blocking_session_id": 200}]
    )
    adapter = PostgreSQLAdapter(executor, "analytics_prod")
    rows = await adapter.blocking()
    assert rows == [{"blocked_session_id": 100, "blocking_session_id": 200}]
    assert "pg_locks" in executor.executed_sql[0]
    assert "pg_stat_activity" in executor.executed_sql[0]


@pytest.mark.asyncio
async def test_postgres_kill_session_calls_pg_terminate_backend():
    executor = FakeQueryExecutor(canned_rows=[])
    adapter = PostgreSQLAdapter(executor, "analytics_prod")
    await adapter.kill_session("9182", "blocking chain")
    assert "pg_terminate_backend" in executor.executed_sql[0]
    assert executor.executed_params[0] == {"pid": 9182}


@pytest.mark.asyncio
async def test_postgres_create_index_never_receives_raw_sql_from_caller():
    """The Agent supplies structured arguments (schema/table/columns/name),
    never a SQL string — the adapter itself is what constructs SQL."""
    executor = FakeQueryExecutor()
    adapter = PostgreSQLAdapter(executor, "analytics_prod")
    await adapter.create_index("dbo", "TransactionPostingHistory", ["TransactionDate"], "IX_TPH_Date", False)
    sql = executor.executed_sql[0]
    assert "create index concurrently" in sql
    assert '"IX_TPH_Date"' in sql
    assert '"TransactionDate"' in sql


@pytest.mark.asyncio
async def test_sqlserver_blocking_uses_dm_exec_requests():
    executor = FakeQueryExecutor(canned_rows=[{"blocked_session_id": 9183}])
    adapter = SQLServerAdapter(executor, "CoreBanking")
    rows = await adapter.blocking()
    assert rows == [{"blocked_session_id": 9183}]
    assert "sys.dm_exec_requests" in executor.executed_sql[0]


@pytest.mark.asyncio
async def test_sqlserver_kill_session_issues_kill_statement():
    executor = FakeQueryExecutor()
    adapter = SQLServerAdapter(executor, "CoreBanking")
    await adapter.kill_session("9182", "blocking chain")
    assert executor.executed_sql[0] == "KILL 9182"


@pytest.mark.asyncio
async def test_sqlserver_kill_session_rejects_non_numeric_session_id():
    """session_id is cast to int before being embedded — a session id like
    '9182; DROP TABLE x' cannot become part of the KILL statement."""
    executor = FakeQueryExecutor()
    adapter = SQLServerAdapter(executor, "CoreBanking")
    with pytest.raises(ValueError):
        await adapter.kill_session("9182; DROP TABLE x", "attempted injection")


@pytest.mark.asyncio
async def test_restart_instance_is_not_a_sql_statement_on_either_adapter():
    """restart/failover require an infrastructure control-plane call, not a
    SQL statement the adapter could construct — this is enforced by raising
    rather than silently no-op'ing."""
    pg = PostgreSQLAdapter(FakeQueryExecutor(), "analytics_prod")
    with pytest.raises(NotImplementedError):
        await pg.restart_instance()

    mssql = SQLServerAdapter(FakeQueryExecutor(), "CoreBanking")
    with pytest.raises(NotImplementedError):
        await mssql.failover("corebanking-prd-02")
