"""Connection-string building and parameter translation for the real
database executors (no DB connection — the live path is covered by the
opt-in tests/e2e/test_live_databases.py)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from inumi.execution.adapters.connections import (
    MySQLQueryExecutor,
    PostgreSQLQueryExecutor,
    SQLServerQueryExecutor,
)
from inumi.execution.credentials.provider import DatabaseCredentials


def _creds(**options) -> DatabaseCredentials:
    return DatabaseCredentials(
        host="db.example",
        port=1433,
        username="svc",
        password="secret",  # noqa: S106 — test value
        database="AW",
        options=options,
    )


def test_connection_string_is_production_safe_by_default():
    cs = SQLServerQueryExecutor(_creds())._connection_string()
    assert "Encrypt=yes" in cs
    assert "TrustServerCertificate=no" in cs
    assert "ODBC Driver 18 for SQL Server" in cs
    assert "SERVER=db.example,1433" in cs
    assert "DATABASE=AW" in cs


def test_trust_server_certificate_option_is_honoured():
    cs = SQLServerQueryExecutor(_creds(trust_server_certificate=True))._connection_string()
    assert "TrustServerCertificate=yes" in cs


def test_encrypt_and_driver_options_are_honoured():
    cs = SQLServerQueryExecutor(
        _creds(encrypt=False, driver="ODBC Driver 17 for SQL Server")
    )._connection_string()
    assert "Encrypt=no" in cs
    assert "ODBC Driver 17 for SQL Server" in cs


def test_named_params_are_translated_to_positional_placeholders():
    sql = "SELECT * FROM t WHERE id = %(id)s AND name = %(name)s AND id2 = %(id)s"
    converted, ordered = SQLServerQueryExecutor._to_positional(sql, {"id": 7, "name": "x"})
    assert converted == "SELECT * FROM t WHERE id = ? AND name = ? AND id2 = ?"
    assert ordered == [7, "x", 7]


def test_no_params_leaves_sql_untouched():
    converted, ordered = SQLServerQueryExecutor._to_positional("SELECT 1", None)
    assert converted == "SELECT 1"
    assert ordered == []


# --- execute() surfaces a SELECTed result value, not just rowcount --------
#
# Reproduces a live bug found while testing the blocking playbook:
# kill_session's `select pg_terminate_backend(%(pid)s) as terminated` always
# came back with terminated=False/missing, no matter what actually happened
# on the server. Every engine's `execute()` discarded the cursor's own
# result row and returned only {"rowcount": ...} — silently wrong, never an
# error, so it went unnoticed across every kill_session/cancel_query call
# this whole project's live testing had ever made.


class _FakeAsyncCursor:
    """Minimal async-context-manager cursor double for the psycopg/asyncmy
    executors — just enough to drive execute()'s result-row-capture logic."""

    def __init__(self, description=None, row=None, rowcount: int = 1):
        self.description = description
        self._row = row
        self.rowcount = rowcount

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, sql, params=None) -> None:
        pass

    async def fetchone(self):
        return self._row


class _FakeAsyncConn:
    def __init__(self, cursor: _FakeAsyncCursor):
        self._cursor = cursor

    def cursor(self) -> _FakeAsyncCursor:
        return self._cursor


class _FakeSyncCursor:
    """Minimal sync cursor double for the pyodbc-backed SQL Server executor."""

    def __init__(self, description=None, row=None, rowcount: int = 1):
        self.description = description
        self._row = row
        self.rowcount = rowcount

    def execute(self, sql, params=None) -> None:
        pass

    def fetchone(self):
        return self._row


class _FakeSyncConn:
    def __init__(self, cursor: _FakeSyncCursor):
        self._cursor = cursor
        self.timeout = None

    def cursor(self) -> _FakeSyncCursor:
        return self._cursor


@pytest.mark.asyncio
async def test_postgres_execute_surfaces_a_selected_columns_value():
    cursor = _FakeAsyncCursor(
        description=[SimpleNamespace(name="terminated")], row=(True,), rowcount=1
    )
    executor = PostgreSQLQueryExecutor(_creds())
    executor._conn = _FakeAsyncConn(cursor)

    result = await executor.execute(
        "select pg_terminate_backend(%(pid)s) as terminated", {"pid": 123}
    )

    assert result["terminated"] is True
    assert result["rowcount"] == 1


@pytest.mark.asyncio
async def test_postgres_execute_with_no_returned_rows_keeps_just_rowcount():
    cursor = _FakeAsyncCursor(description=None, row=None, rowcount=1)
    executor = PostgreSQLQueryExecutor(_creds())
    executor._conn = _FakeAsyncConn(cursor)

    result = await executor.execute("update t set x = 1")

    assert result == {"rowcount": 1}


@pytest.mark.asyncio
async def test_mysql_execute_surfaces_a_selected_columns_value():
    cursor = _FakeAsyncCursor(description=[("cancelled",)], row=(1,), rowcount=1)
    executor = MySQLQueryExecutor(_creds())
    executor._conn = _FakeAsyncConn(cursor)

    result = await executor.execute("select 1 as cancelled")

    assert result["cancelled"] == 1


@pytest.mark.asyncio
async def test_sqlserver_execute_surfaces_a_selected_columns_value():
    cursor = _FakeSyncCursor(description=[("terminated", None)], row=(1,), rowcount=1)
    executor = SQLServerQueryExecutor(_creds())
    executor._conn = _FakeSyncConn(cursor)

    result = await executor.execute("select 1 as terminated")

    assert result["terminated"] == 1
