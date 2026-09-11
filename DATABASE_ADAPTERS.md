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

`pyodbc` and `psycopg` are part of the base install. `pyodbc` also needs
the platform ODBC driver at *runtime* (the `Dockerfile` installs Microsoft
ODBC Driver 18); it is not needed to install the package or run the tests.

## The Execution Service always uses real connections

There is no "mock execution mode". `ExecutionService` resolves a real
credential via `CredentialProvider` and opens a real connection for every
request.

For the automated pipeline tests (policy / risk / approval / audit), which
need *deterministic* execution results but have nothing to do with SQL,
`ExecutionService` exposes one test seam: an optional `adapter_factory`.
`tests/canned_adapter.py` uses it to inject a `CannedDatabaseAdapter` (a
`DatabaseAdapter` subclass returning the spec's canonical scenario — a
43-session blocking chain headed by `9182`). That fake lives in `tests/`,
never in `src/`, so the shipped service has no fake-data path.

Real adapter *query text* is covered against `FakeQueryExecutor` in
`tests/unit/test_adapters.py`, and against live engines by the opt-in
`tests/e2e/test_live_databases.py` (`docker compose up -d postgres-sample`,
or `docker compose --profile mssql up -d mssql-sample`).

## Adding Oracle / MariaDB

1. Add the platform to `common.models.target.Platform`.
2. Implement a new `DatabaseAdapter` subclass using that engine's native
   diagnostics (e.g., Oracle's `V$SESSION`/`V$LOCK`/AWR views, MariaDB's
   `information_schema`/`performance_schema`).
3. Implement a `QueryExecutor` for its native driver (or reuse an ODBC
   path).
4. Implement a `ServerDiscoverer` (`execution/discovery/base.py`) that reads
   the engine's catalog/stats views — database list, objects, extensions —
   and never any table data.
5. Register the platform → adapter mapping in
   `execution/service.py::_adapter_class_for` and the discoverer in
   `execution/discovery/engine.py::_DISCOVERERS`.
6. Register servers of the new platform in `config/servers.yaml`. The
   databases and objects under each are discovered automatically.

The Agent and Gateway contracts (`ToolDefinition`, `DatabaseTarget`,
`ExecutionRequest`) do not change.
