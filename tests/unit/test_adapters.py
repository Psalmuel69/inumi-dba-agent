from __future__ import annotations

import pytest

from inumi.execution.adapters.mysql import MySQLAdapter
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
async def test_postgres_top_queries_degrades_when_pg_stat_statements_is_missing():
    """Reproduces a live finding: pg_stat_statements is an optional
    extension (the module docstring already says "where installed"), but
    a server without it enabled used to bare-fail with EXECUTION_FAILED
    and no diagnosable reason. Must degrade to an informative row instead."""
    executor = FakeQueryExecutor(
        fetch_error=RuntimeError('relation "pg_stat_statements" does not exist')
    )
    adapter = PostgreSQLAdapter(executor, "TestDatabase")
    rows = await adapter.top_queries("cpu", 10)
    assert len(rows) == 1
    assert "pg_stat_statements" in rows[0]["note"]


@pytest.mark.asyncio
async def test_postgres_query_plan_degrades_when_pg_stat_statements_is_missing():
    executor = FakeQueryExecutor(
        fetch_error=RuntimeError('relation "pg_stat_statements" does not exist')
    )
    adapter = PostgreSQLAdapter(executor, "TestDatabase")
    rows = await adapter.query_plan("12345")
    assert len(rows) == 1
    assert "pg_stat_statements" in rows[0]["note"]


@pytest.mark.asyncio
async def test_postgres_top_queries_does_not_swallow_an_unrelated_failure():
    """Only the known-optional pg_stat_statements dependency degrades — a
    real connection/permissions/syntax problem must still propagate."""
    executor = FakeQueryExecutor(fetch_error=RuntimeError("connection reset by peer"))
    adapter = PostgreSQLAdapter(executor, "TestDatabase")
    with pytest.raises(RuntimeError, match="connection reset"):
        await adapter.top_queries("cpu", 10)


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

    mysql = MySQLAdapter(FakeQueryExecutor(), "app_db")
    with pytest.raises(NotImplementedError):
        await mysql.restart_instance()


@pytest.mark.asyncio
async def test_mysql_blocking_uses_data_lock_waits_and_innodb_trx():
    executor = FakeQueryExecutor(canned_rows=[{"blocked_session_id": 42, "blocking_session_id": 7}])
    adapter = MySQLAdapter(executor, "app_db")
    rows = await adapter.blocking()
    assert rows == [{"blocked_session_id": 42, "blocking_session_id": 7}]
    assert "performance_schema.data_lock_waits" in executor.executed_sql[0]
    assert "information_schema.INNODB_TRX" in executor.executed_sql[0]


@pytest.mark.asyncio
async def test_mysql_kill_session_issues_kill_statement():
    executor = FakeQueryExecutor()
    adapter = MySQLAdapter(executor, "app_db")
    await adapter.kill_session("9182", "blocking chain")
    assert executor.executed_sql[0] == "KILL 9182"


@pytest.mark.asyncio
async def test_mysql_kill_session_rejects_non_numeric_session_id():
    executor = FakeQueryExecutor()
    adapter = MySQLAdapter(executor, "app_db")
    with pytest.raises(ValueError):
        await adapter.kill_session("9182; DROP TABLE x", "attempted injection")


@pytest.mark.asyncio
async def test_mysql_create_index_never_receives_raw_sql_from_caller():
    executor = FakeQueryExecutor()
    adapter = MySQLAdapter(executor, "app_db")
    await adapter.create_index("app_db", "orders", ["created_at"], "IX_orders_created", False)
    sql = executor.executed_sql[0]
    assert "CREATE INDEX" in sql
    assert "`IX_orders_created`" in sql
    assert "`created_at`" in sql


@pytest.mark.asyncio
async def test_mysql_deadlocks_extracts_the_latest_detected_section():
    status_text = (
        "=====================================\n"
        "LATEST DETECTED DEADLOCK\n"
        "------------------------\n"
        "*** (1) TRANSACTION:\nsome transaction detail\n"
        "------------\n"
        "WE ROLL BACK TRANSACTION (1)\n"
        "-----------------------------------------\n"
        "END OF INNODB MONITOR OUTPUT\n"
    )
    executor = FakeQueryExecutor(canned_rows=[{"Type": "InnoDB", "Name": "", "Status": status_text}])
    adapter = MySQLAdapter(executor, "app_db")
    rows = await adapter.deadlocks()
    assert "some transaction detail" in rows[0]["latest_detected_deadlock"]


@pytest.mark.asyncio
async def test_mysql_backups_has_no_builtin_catalog():
    adapter = MySQLAdapter(FakeQueryExecutor(), "app_db")
    with pytest.raises(NotImplementedError):
        await adapter.backups()
