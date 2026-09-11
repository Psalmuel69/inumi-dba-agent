"""Test doubles shared across the test suite.

`FakeQueryExecutor` stands in for a live database connection so adapter
*logic* (which DMV/pg_stat query is issued for which tool, how raw rows are
shaped) can be verified without a real SQL Server or PostgreSQL instance
(spec §32/§67).
"""

from __future__ import annotations

from typing import Any


class FakeQueryExecutor:
    def __init__(
        self, canned_rows: list[dict[str, Any]] | None = None, *, fetch_error: Exception | None = None
    ):
        self.canned_rows = canned_rows if canned_rows is not None else []
        self.fetch_error = fetch_error
        self.executed_sql: list[str] = []
        self.executed_params: list[dict[str, Any] | None] = []

    async def fetch_all(
        self, sql: str, params: dict[str, Any] | None = None, *, timeout: int = 30
    ) -> list[dict[str, Any]]:
        self.executed_sql.append(sql)
        self.executed_params.append(params)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.canned_rows

    async def execute(
        self, sql: str, params: dict[str, Any] | None = None, *, timeout: int = 30
    ) -> dict[str, Any]:
        self.executed_sql.append(sql)
        self.executed_params.append(params)
        return {"rowcount": 1}
