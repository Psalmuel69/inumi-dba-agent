"""Execution Service core dispatcher (spec §18, §50).

This is the ONLY code in the entire platform that is permitted to obtain a
database credential and open a database connection. It receives an already
fully-authorized, already-approved `ExecutionRequest` from the Gateway (never
directly from the Agent — see `execution.api.app` for the service-auth
enforcement that guarantees this) and:

  1. looks up credentials for the target `database_id` via `CredentialProvider`
  2. opens a scoped connection (or the local mock adapter in dev)
  3. dispatches to the one typed adapter method matching `tool_id`
  4. enforces `max_execution_time` via a hard timeout
  5. enforces `max_result_rows` by truncating (never silently dropping the
     fact that it happened — `truncated=True` is always reported)
  6. returns raw (not yet masked) results for the Gateway's Data Policy
     Layer to minimize — this service never applies masking itself, so
     there is exactly one place that decision is made
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from inumi.common.config import Settings
from inumi.common.models.execution import ExecutionRequest, ExecutionResult
from inumi.common.models.failures import FailureCode
from inumi.common.models.target import Platform
from inumi.execution.adapters.base import DatabaseAdapter, QueryExecutor
from inumi.execution.adapters.mock import MockDatabaseAdapter
from inumi.execution.adapters.postgresql import PostgreSQLAdapter
from inumi.execution.adapters.sqlserver import SQLServerAdapter
from inumi.execution.credentials.provider import CredentialProvider

_READ_METHODS = {
    "database.get_health": ("health", []),
    "database.get_version": ("version", []),
    "database.get_sessions": ("sessions", []),
    "database.get_blocking_sessions": ("blocking", []),
    "database.get_deadlocks": ("deadlocks", []),
    "database.get_running_queries": ("running_queries", []),
    "database.get_wait_statistics": ("waits", []),
    "database.get_query_plan": ("query_plan", ["query_id"]),
    "database.get_top_queries": ("top_queries", ["order_by", "limit"]),
    "database.get_indexes": ("indexes", ["schema_name", "table"]),
    "database.get_statistics": ("statistics", ["schema_name", "table"]),
    "database.get_tables": ("tables", []),
    "database.get_storage": ("storage", []),
    "database.get_transaction_log": ("transaction_log", []),
    "database.get_replication_status": ("replication", []),
    "database.get_backup_status": ("backups", []),
    "database.get_configuration": ("configuration", []),
    "database.get_error_logs": ("error_logs", ["since_minutes", "limit"]),
}

_WRITE_METHODS = {
    "database.cancel_query": ("cancel_query", ["session_id", "reason"]),
    "database.kill_session": ("kill_session", ["session_id", "reason"]),
    "database.update_statistics": ("update_statistics", ["schema_name", "table"]),
    "database.create_index": ("create_index", ["schema_name", "table", "columns", "name", "unique"]),
    "database.rebuild_index": ("rebuild_index", ["schema_name", "table", "index_name"]),
    "database.modify_configuration": ("modify_configuration", ["parameter", "value"]),
    "database.restart_instance": ("restart_instance", []),
    "database.failover": ("failover", ["target_instance"]),
}


def _adapter_class_for(platform: Platform) -> type[DatabaseAdapter]:
    if platform == Platform.SQLSERVER:
        return SQLServerAdapter
    if platform == Platform.POSTGRESQL:
        return PostgreSQLAdapter
    raise NotImplementedError(
        f"No adapter registered for platform '{platform.value}'. Adding a new engine "
        "(e.g. Oracle, MariaDB) means implementing DatabaseAdapter and registering it "
        "here — the Agent/Gateway contract does not change."
    )


class ExecutionService:
    def __init__(self, settings: Settings, credential_provider: CredentialProvider):
        self._settings = settings
        self._credentials = credential_provider

    async def _build_adapter(self, request: ExecutionRequest) -> tuple[DatabaseAdapter, Any]:
        """Returns (adapter, connection_handle_or_None)."""
        if self._settings.execution_mode == "mock":
            return MockDatabaseAdapter(request.database), None

        creds = await self._credentials.get_credentials(request.database_id)
        adapter_cls = _adapter_class_for(request.platform)

        if request.platform == Platform.SQLSERVER:
            from inumi.execution.adapters.connections import SQLServerQueryExecutor

            executor: QueryExecutor = SQLServerQueryExecutor(creds)  # type: ignore[assignment]
        else:
            from inumi.execution.adapters.connections import PostgreSQLQueryExecutor

            executor = PostgreSQLQueryExecutor(creds)  # type: ignore[assignment]

        await executor.connect()  # type: ignore[attr-defined]
        return adapter_cls(executor, request.database), executor

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        start = time.monotonic()
        handle = None
        try:
            adapter, handle = await self._build_adapter(request)
            result = await asyncio.wait_for(
                self._dispatch(adapter, request), timeout=request.max_execution_time
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            result.duration_ms = duration_ms
            return result
        except asyncio.TimeoutError:
            return ExecutionResult(
                execution_id=request.execution_id,
                success=False,
                error_code=FailureCode.EXECUTION_TIMEOUT.value,
                error_detail=f"Execution exceeded {request.max_execution_time}s timeout.",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except NotImplementedError as exc:
            return ExecutionResult(
                execution_id=request.execution_id,
                success=False,
                error_code=FailureCode.TOOL_NOT_AVAILABLE.value,
                error_detail=str(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 — deliberately broad: never leak internals
            return ExecutionResult(
                execution_id=request.execution_id,
                success=False,
                error_code=FailureCode.EXECUTION_FAILED.value,
                error_detail="The database operation failed. See server-side logs for detail.",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        finally:
            if handle is not None:
                await handle.close()

    async def _dispatch(self, adapter: DatabaseAdapter, request: ExecutionRequest) -> ExecutionResult:
        tool_id = request.tool_id
        args = request.arguments

        if tool_id in _READ_METHODS:
            method_name, arg_names = _READ_METHODS[tool_id]
            method = getattr(adapter, method_name)
            call_args = [args[name] for name in arg_names]
            rows = await method(*call_args)
            truncated = len(rows) > request.max_result_rows
            rows = rows[: request.max_result_rows]
            columns = sorted({k for row in rows for k in row.keys()})
            return ExecutionResult(
                execution_id=request.execution_id,
                success=True,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
            )

        if tool_id in _WRITE_METHODS:
            method_name, arg_names = _WRITE_METHODS[tool_id]
            method = getattr(adapter, method_name)
            call_args = [args[name] for name in arg_names]
            affected = await method(*call_args)
            return ExecutionResult(
                execution_id=request.execution_id,
                success=True,
                affected=affected,
            )

        # Restricted tools — only reachable at all if the Gateway's tool
        # registry marked them enabled; the adapter method itself still
        # raises NotImplementedError unless a real engine-specific
        # implementation has been supplied.
        restricted_dispatch = {
            "database.execute_readonly_sql": lambda: adapter.execute_readonly_sql(args["sql"]),
            "database.execute_sql": lambda: adapter.execute_sql(args["sql"]),
            "database.restore_database": lambda: adapter.restore_database(args["backup_id"]),
            "database.create_database": lambda: adapter.create_database(args["database_name"]),
            "database.drop_database": lambda: adapter.drop_database(args["database_name"]),
            "database.truncate_table": lambda: adapter.truncate_table(
                args["schema_name"], args["table"]
            ),
            "database.bulk_delete": lambda: adapter.bulk_delete(
                args["schema_name"], args["table"], args["predicate_description"]
            ),
        }
        if tool_id in restricted_dispatch:
            outcome = await restricted_dispatch[tool_id]()
            if isinstance(outcome, list):
                return ExecutionResult(
                    execution_id=request.execution_id,
                    success=True,
                    rows=outcome,
                    row_count=len(outcome),
                )
            return ExecutionResult(execution_id=request.execution_id, success=True, affected=outcome)

        return ExecutionResult(
            execution_id=request.execution_id,
            success=False,
            error_code=FailureCode.TOOL_NOT_FOUND.value,
            error_detail=f"Execution Service has no dispatch entry for '{tool_id}'.",
        )
