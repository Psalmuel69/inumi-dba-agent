"""Discovery dispatch + catalog store (no DB — the live crawl is covered by
the opt-in tests/e2e/test_live_databases.py)."""

from __future__ import annotations

import datetime as dt

import pytest

from inumi.common.models.catalog import DiscoveredDatabase, DiscoveredObject, ServerCatalog
from inumi.common.models.target import Platform
from inumi.execution.discovery.engine import _discoverer_for
from inumi.execution.discovery.mysql import MySQLDiscoverer
from inumi.execution.discovery.postgresql import PostgreSQLDiscoverer
from inumi.execution.discovery.sqlserver import SQLServerDiscoverer, _quote


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
