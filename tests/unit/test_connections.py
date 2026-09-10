"""Connection-string building and parameter translation for the real
database executors (no DB connection — the live path is covered by the
opt-in tests/e2e/test_live_databases.py)."""

from __future__ import annotations

from inumi.execution.adapters.connections import SQLServerQueryExecutor
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
