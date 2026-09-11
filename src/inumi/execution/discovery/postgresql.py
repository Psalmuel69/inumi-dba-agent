"""PostgreSQL discovery (spec §20/§48).

PostgreSQL binds a connection to a single database, so this opens one
connection to the bootstrap database to enumerate databases + server
settings, then a fresh connection per database for its objects.
"""

from __future__ import annotations

from inumi.common.models.catalog import (
    DiscoveredDatabase,
    DiscoveredExtension,
    DiscoveredObject,
    ServerCatalog,
)
from inumi.execution.discovery.base import ServerDiscoverer, _fetch, _now


class PostgreSQLDiscoverer(ServerDiscoverer):
    async def discover(self, server_id: str) -> ServerCatalog:
        from inumi.execution.adapters.connections import PostgreSQLQueryExecutor

        warnings: list[str] = []
        boot = PostgreSQLQueryExecutor(self._credentials)
        await boot.connect()
        try:
            version = (await _fetch(boot, "SHOW server_version"))[0].get("server_version", "")
            settings_rows = await _fetch(
                boot, "SELECT name, setting, unit FROM pg_settings ORDER BY name"
            )
            instance_properties = {
                "settings": {
                    r["name"]: (f"{r['setting']}{r['unit']}" if r.get("unit") else r["setting"])
                    for r in settings_rows
                },
            }
            db_rows = await _fetch(
                boot,
                "SELECT datname, pg_get_userbyid(datdba) AS owner, "
                "pg_encoding_to_char(encoding) AS encoding, datcollate "
                "FROM pg_database WHERE datistemplate = false AND datallowconn ORDER BY datname",
            )
        finally:
            await boot.close()

        databases: list[DiscoveredDatabase] = []
        for row in db_rows:
            db_name = row["datname"]
            db = DiscoveredDatabase(
                name=db_name,
                state="online",
                options={
                    "owner": row.get("owner"),
                    "encoding": row.get("encoding"),
                    "collation": row.get("datcollate"),
                },
            )
            try:
                await self._fill_database(db, db_name)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"could not read database {db_name}: {exc}")
            databases.append(db)

        return ServerCatalog(
            server_id=server_id,
            discovered_at=_now(),
            engine_version=version,
            engine_edition="PostgreSQL",
            instance_properties=instance_properties,
            databases=databases,
            warnings=warnings,
        )

    async def _fill_database(self, db: DiscoveredDatabase, db_name: str) -> None:
        from inumi.execution.adapters.connections import PostgreSQLQueryExecutor

        creds = self._credentials.model_copy(update={"database": db_name})
        ex = PostgreSQLQueryExecutor(creds)
        await ex.connect()
        try:
            db.size_bytes = (await _fetch(ex, "SELECT pg_database_size(current_database()) AS s"))[
                0
            ].get("s")

            tables = await _fetch(
                ex,
                "SELECT n.nspname AS schema_name, c.relname AS name, "
                "  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'table' "
                "                 WHEN 'v' THEN 'view' WHEN 'm' THEN 'view' END AS kind, "
                "  s.n_live_tup AS row_estimate, "
                "  pg_total_relation_size(c.oid) AS size_bytes "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
                "WHERE c.relkind IN ('r','p','v','m') "
                "  AND n.nspname NOT IN ('pg_catalog','information_schema') "
                "ORDER BY pg_total_relation_size(c.oid) DESC NULLS LAST "
                "LIMIT %(limit)s",
                {"limit": self._max_objects},
            )
            indexes = await _fetch(
                ex,
                "SELECT s.schemaname AS schema_name, s.indexrelname AS name, 'index' AS kind, "
                "  s.relname AS table_name, s.idx_scan, s.idx_tup_read, s.idx_tup_fetch, "
                "  pg_relation_size(s.indexrelid) AS size_bytes "
                "FROM pg_stat_user_indexes s ORDER BY s.idx_scan DESC LIMIT %(limit)s",
                {"limit": self._max_objects},
            )
            routines = await _fetch(
                ex,
                "SELECT n.nspname AS schema_name, p.proname AS name, "
                "  CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END AS kind "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "ORDER BY p.proname LIMIT %(limit)s",
                {"limit": self._max_objects},
            )
            ext_rows = await _fetch(
                ex,
                "SELECT ae.name, ae.default_version, ae.installed_version, ae.comment "
                "FROM pg_available_extensions ae ORDER BY ae.name",
            )

            for r in tables:
                db.objects.append(
                    DiscoveredObject(
                        schema_name=r["schema_name"], name=r["name"], kind=r["kind"] or "table",
                        row_estimate=r.get("row_estimate"), size_bytes=r.get("size_bytes"),
                    )
                )
            for r in indexes:
                db.objects.append(
                    DiscoveredObject(
                        schema_name=r["schema_name"], name=r["name"], kind="index",
                        size_bytes=r.get("size_bytes"),
                        properties={
                            "table": r.get("table_name"),
                            "idx_scan": r.get("idx_scan"),
                            "idx_tup_read": r.get("idx_tup_read"),
                            "idx_tup_fetch": r.get("idx_tup_fetch"),
                        },
                    )
                )
            for r in routines:
                db.objects.append(
                    DiscoveredObject(schema_name=r["schema_name"], name=r["name"], kind=r["kind"])
                )
            db.extensions = [
                DiscoveredExtension(
                    name=r["name"],
                    default_version=r.get("default_version"),
                    installed_version=r.get("installed_version"),
                    available=True,
                )
                for r in ext_rows
            ]
        finally:
            await ex.close()
