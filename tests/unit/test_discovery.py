"""Discovery dispatch + catalog store (no DB — the live crawl is covered by
the opt-in tests/e2e/test_live_databases.py)."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from inumi.common.models.catalog import DiscoveredDatabase, DiscoveredObject, ServerCatalog
from inumi.common.models.execution import DiscoveryRequest, ExecutionRequest, ExecutionResult
from inumi.common.models.target import Platform
from inumi.execution.discovery.engine import _discoverer_for
from inumi.execution.discovery.mysql import MySQLDiscoverer
from inumi.execution.discovery.postgresql import PostgreSQLDiscoverer
from inumi.execution.discovery.sqlserver import SQLServerDiscoverer, _quote
from inumi.gateway.domain.discovery import DiscoveryOrchestrator, clean_discovery_error
from inumi.gateway.infrastructure.execution_client import ExecutionClient


def test_dispatch_picks_the_right_discoverer():
    assert _discoverer_for(Platform.SQLSERVER) is SQLServerDiscoverer
    assert _discoverer_for(Platform.POSTGRESQL) is PostgreSQLDiscoverer
    assert _discoverer_for(Platform.MYSQL) is MySQLDiscoverer
    assert _discoverer_for(Platform.MARIADB) is MySQLDiscoverer


def test_dispatch_raises_for_an_unregistered_engine():
    with pytest.raises(NotImplementedError):
        _discoverer_for(Platform.ORACLE)


def test_sqlserver_identifier_quoting_is_injection_safe():
    assert _quote("AdventureWorks2019") == "[AdventureWorks2019]"
    assert _quote("weird]name") == "[weird]]name]"


def test_catalog_lookup_is_case_insensitive_and_returns_canonical_name():
    cat = ServerCatalog(
        server_id="s1",
        databases=[
            DiscoveredDatabase(
                name="CoreBanking",
                objects=[DiscoveredObject(schema_name="dbo", name="Accounts", kind="table")],
            )
        ],
    )
    assert cat.database("corebanking").name == "CoreBanking"
    assert cat.database("nope") is None
    assert cat.database_names() == ["CoreBanking"]


class _FailingExecutionClient(ExecutionClient):
    """Always raises the given exception from `discover()` — used to verify
    `DiscoveryOrchestrator` never lets a raw exception string reach the DBA
    (live-reproduced finding: an `httpx.HTTPStatusError`'s own `__str__`
    bakes in the raw request URL and an MDN documentation link)."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:  # pragma: no cover
        raise NotImplementedError

    async def discover(self, request: DiscoveryRequest):
        raise self._exc


def _http_500_error() -> httpx.HTTPStatusError:
    """A realistic reproduction of the live finding: httpx's own message
    for a raised `raise_for_status()` includes the raw URL and an MDN link."""
    request = httpx.Request("POST", "http://localhost:8002/v1/discover")
    response = httpx.Response(500, request=request, text="Internal Server Error")
    return httpx.HTTPStatusError(
        "Server error '500 Internal Server Error' for url 'http://localhost:8002/v1/discover'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/500",
        request=request,
        response=response,
    )


class TestCleanDiscoveryError:
    def test_http_status_error_becomes_a_clean_status_message(self):
        assert clean_discovery_error(_http_500_error()) == (
            "execution service returned an error (status 500)"
        )

    def test_connect_error_becomes_a_clean_unreachable_message(self):
        request = httpx.Request("POST", "http://localhost:8002/v1/discover")
        assert (
            clean_discovery_error(httpx.ConnectError("boom", request=request))
            == "could not reach the execution service"
        )
        assert (
            clean_discovery_error(httpx.ConnectTimeout("boom", request=request))
            == "could not reach the execution service"
        )

    def test_timeout_errors_become_a_clean_timeout_message(self):
        request = httpx.Request("POST", "http://localhost:8002/v1/discover")
        assert clean_discovery_error(httpx.ReadTimeout("boom", request=request)) == "discovery timed out"
        assert clean_discovery_error(httpx.TimeoutException("boom")) == "discovery timed out"

    def test_an_unrecognized_exception_gets_a_generic_clean_fallback(self):
        assert clean_discovery_error(ValueError("some internal detail")) == "discovery failed (ValueError)"


class TestRefreshAllNeverLeaksRawExceptionText:
    async def test_a_failing_server_gets_a_clean_message_not_the_raw_httpx_text(
        self, server_registry, catalog_store, settings
    ):
        exc = _http_500_error()
        orchestrator = DiscoveryOrchestrator(
            registry=server_registry,
            catalog_store=catalog_store,
            execution_client=_FailingExecutionClient(exc),
            settings=settings,
        )

        results = await orchestrator.refresh_all()

        assert results  # config/servers.yaml has at least one active server
        for server_id, message in results.items():
            assert message == "failed: execution service returned an error (status 500)", server_id
            assert "developer.mozilla.org" not in message
            assert "localhost:8002" not in message
            assert "raise_for_status" not in message

    async def test_a_connect_error_also_gets_a_clean_message(
        self, server_registry, catalog_store, settings
    ):
        request = httpx.Request("POST", "http://localhost:8002/v1/discover")
        orchestrator = DiscoveryOrchestrator(
            registry=server_registry,
            catalog_store=catalog_store,
            execution_client=_FailingExecutionClient(httpx.ConnectError("boom", request=request)),
            settings=settings,
        )

        results = await orchestrator.refresh_all()

        assert results
        for message in results.values():
            assert message == "failed: could not reach the execution service"


async def test_db_catalog_store_round_trips_through_the_control_db(db):
    from inumi.gateway.infrastructure.catalog_store import DbCatalogStore

    store = DbCatalogStore(db.session_factory)
    cat = ServerCatalog(
        server_id="s1",
        discovered_at=dt.datetime.now(dt.UTC),
        engine_version="16.0",
        databases=[DiscoveredDatabase(name="AppDB", size_bytes=123)],
    )
    await store.put(cat)

    # Fresh store instance -> must load from the DB, not memory.
    store2 = DbCatalogStore(db.session_factory)
    loaded = await store2.get("s1")
    assert loaded is not None
    assert loaded.engine_version == "16.0"
    assert loaded.databases[0].name == "AppDB"
    assert loaded.databases[0].size_bytes == 123
