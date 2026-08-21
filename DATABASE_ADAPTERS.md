# Database Adapters

## Interface

`execution/adapters/base.py::DatabaseAdapter` declares every method spec §20
requires (health, version, sessions, blocking, deadlocks, running_queries,
waits, query_plan, top_queries, indexes, statistics, tables, storage,
transaction_log, replication, backups, configuration, error_logs,
cancel_query, kill_session, update_statistics, create_index, rebuild_index,
modify_configuration, restart_instance, failover), plus the restricted
operations (execute_readonly_sql, execute_sql, restore/create/drop
database, truncate_table, bulk_delete) with `NotImplementedError` defaults.

An adapter is constructed with a `QueryExecutor` (a thin protocol wrapping
an actual connection) and a database name — **it never receives a
credential directly**; that's resolved by `execution/service.py` via
`CredentialProvider` before the adapter is even instantiated.

## SQL Server (`execution/adapters/sqlserver.py`)

Uses `sys.dm_exec_*`, `sys.dm_os_*`, `sys.dm_tran_*`, `sys.dm_hadr_*`,
Query Store catalog views (`sys.query_store_*`), and `sys.configurations`/
`sys.databases`. Session termination uses `KILL <id>` with `session_id` cast
to `int` before interpolation (so `"9182; DROP TABLE x"` raises `ValueError`
rather than reaching the connection). Index/statistics operations use
`WITH (ONLINE = ON)` where applicable. `restart_instance`/`failover` raise
`NotImplementedError` deliberately — they require an out-of-band
infrastructure action (SQL Server Agent job, Always On runbook, cloud
instance API), never a T-SQL statement an adapter should construct itself.

## PostgreSQL (`execution/adapters/postgresql.py`)

Uses `pg_stat_activity`, `pg_locks`, `pg_stat_statements` (top queries/plan
metadata), `pg_stat_user_tables`/`pg_stat_user_indexes` (autovacuum/index
usage), `pg_stat_replication`/`pg_stat_archiver` (replication/WAL), and
`pg_settings`. Session control uses `pg_terminate_backend`/
`pg_cancel_backend` with the pid cast to `int`. Index creation/rebuild use
`CONCURRENTLY` to avoid blocking. `restart_instance`/`failover` similarly
raise `NotImplementedError` — that's Patroni/repmgr/cloud-managed-failover
territory.

## Connections (`execution/adapters/connections.py`)

`PostgreSQLQueryExecutor` wraps a native async `psycopg` (v3) connection,
setting `statement_timeout`/`lock_timeout` per call (spec §48).
`SQLServerQueryExecutor` wraps a synchronous `pyodbc` connection run via
`asyncio.to_thread`, translating the adapters' `%(name)s`-style parameters
to pyodbc's positional `?` placeholders.

Both drivers are optional extras (`pip install -e ".[db-drivers]"`) so the
rest of the platform — and the entire automated test suite — works without
an ODBC driver or a live database.

## Mock mode (`execution/adapters/mock.py`)

`EXECUTION_MODE=mock` (the default) uses `MockDatabaseAdapter`, which
returns clearly-synthetic, deterministic data shaped like the spec's
worked examples (e.g., 43 blocking sessions headed by session `9182`) so
the whole system — including the acceptance-scenario tests — runs without
any real database server. Production always uses `EXECUTION_MODE=real`.

## Adding Oracle / MariaDB

1. Add the platform to `common.models.target.Platform`.
2. Implement a new `DatabaseAdapter` subclass using that engine's native
   diagnostics (e.g., Oracle's `V$SESSION`/`V$LOCK`/AWR views, MariaDB's
   `information_schema`/`performance_schema`).
3. Implement a `QueryExecutor` for its native driver (or reuse an ODBC
   path).
4. Register the platform → adapter mapping in
   `execution/service.py::_adapter_class_for`.
5. Add inventory entries with the new platform in `config/inventory.yaml`.

The Agent and Gateway contracts (`ToolDefinition`, `DatabaseTarget`,
`ExecutionRequest`) do not change.
