"""SQL Server discovery (spec §20/§48)."""

from __future__ import annotations

from inumi.common.models.catalog import (
    DiscoveredDatabase,
    DiscoveredObject,
    ServerCatalog,
)
from inumi.execution.discovery.base import ServerDiscoverer, _fetch, _now


def _quote(identifier: str) -> str:
    return "[" + identifier.replace("]", "]]") + "]"


class SQLServerDiscoverer(ServerDiscoverer):
    async def discover(self, server_id: str) -> ServerCatalog:
        from inumi.execution.adapters.connections import SQLServerQueryExecutor

        ex = SQLServerQueryExecutor(self._credentials)
        await ex.connect()
        warnings: list[str] = []
        try:
            props = (
                await _fetch(
                    ex,
                    "SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128)) AS version, "
                    "CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128)) AS edition, "
                    "CAST(SERVERPROPERTY('ProductLevel') AS NVARCHAR(128)) AS level, "
                    "CAST(SERVERPROPERTY('IsHadrEnabled') AS INT) AS hadr_enabled, "
                    "(SELECT cpu_count FROM sys.dm_os_sys_info) AS cpu_count",
                )
            )[0]

            configs = await _fetch(
                ex,
                "SELECT name, CAST(value_in_use AS NVARCHAR(4000)) AS value "
                "FROM sys.configurations ORDER BY name",
            )
            instance_properties = {
                "cpu_count": props.get("cpu_count"),
                "hadr_enabled": bool(props.get("hadr_enabled")),
                "product_level": props.get("level"),
                "configuration": {c["name"]: c["value"] for c in configs},
            }

            db_rows = await _fetch(
                ex,
                "SELECT d.name, d.state_desc, d.recovery_model_desc, d.compatibility_level, "
                "d.is_read_only, d.is_encrypted, d.collation_name, "
                "(SELECT SUM(CAST(mf.size AS BIGINT)) * 8 * 1024 FROM sys.master_files mf "
                " WHERE mf.database_id = d.database_id) AS size_bytes "
                "FROM sys.databases d WHERE d.name <> 'tempdb' ORDER BY d.name",
            )

            databases: list[DiscoveredDatabase] = []
            for row in db_rows:
                db_name = row["name"]
                db = DiscoveredDatabase(
                    name=db_name,
                    state=(row.get("state_desc") or "unknown").lower(),
                    size_bytes=row.get("size_bytes"),
                    options={
                        "recovery_model": row.get("recovery_model_desc"),
                        "compatibility_level": row.get("compatibility_level"),
                        "read_only": bool(row.get("is_read_only")),
                        "encrypted": bool(row.get("is_encrypted")),
                        "collation": row.get("collation_name"),
                    },
                )
                if db.state == "online":
                    try:
                        db.objects = await self._objects(ex, db_name)
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"could not read objects in {db_name}: {exc}")
                databases.append(db)

            return ServerCatalog(
                server_id=server_id,
                discovered_at=_now(),
                engine_version=props.get("version", ""),
                engine_edition=props.get("edition", ""),
                instance_properties=instance_properties,
                databases=databases,
                warnings=warnings,
            )
        finally:
            await ex.close()

    async def _objects(self, ex, db_name: str) -> list[DiscoveredObject]:
        q = _quote(db_name)
        tables = await _fetch(
            ex,
            f"SELECT TOP (%(limit)s) s.name AS schema_name, t.name AS name, 'table' AS kind, "
            f"       p.rows AS row_estimate, "
            f"       SUM(CAST(a.total_pages AS BIGINT)) * 8 * 1024 AS size_bytes "
            f"FROM {q}.sys.tables t "
            f"JOIN {q}.sys.schemas s ON s.schema_id = t.schema_id "
            f"JOIN {q}.sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1) "
            f"JOIN {q}.sys.allocation_units a ON a.container_id = p.partition_id "
            f"GROUP BY s.name, t.name, p.rows "
            f"ORDER BY p.rows DESC",
            {"limit": self._max_objects},
        )
        views = await _fetch(
            ex,
            f"SELECT TOP (%(limit)s) s.name AS schema_name, v.name AS name, 'view' AS kind "
            f"FROM {q}.sys.views v JOIN {q}.sys.schemas s ON s.schema_id = v.schema_id "
            f"ORDER BY v.name",
            {"limit": self._max_objects},
        )
        indexes = await _fetch(
            ex,
            f"SELECT TOP (%(limit)s) s.name AS schema_name, "
            f"       i.name AS name, 'index' AS kind, "
            f"       i.type_desc AS index_type, t.name AS table_name, "
            f"       us.user_seeks, us.user_scans, us.user_lookups, us.user_updates "
            f"FROM {q}.sys.indexes i "
            f"JOIN {q}.sys.tables t ON t.object_id = i.object_id "
            f"JOIN {q}.sys.schemas s ON s.schema_id = t.schema_id "
            f"LEFT JOIN {q}.sys.dm_db_index_usage_stats us "
            f"       ON us.object_id = i.object_id AND us.index_id = i.index_id "
            f"WHERE i.name IS NOT NULL "
            f"ORDER BY i.name",
            {"limit": self._max_objects},
        )
        routines = await _fetch(
            ex,
            f"SELECT TOP (%(limit)s) s.name AS schema_name, o.name AS name, "
            f"  CASE o.type WHEN 'P' THEN 'procedure' ELSE 'function' END AS kind "
            f"FROM {q}.sys.objects o JOIN {q}.sys.schemas s ON s.schema_id = o.schema_id "
            f"WHERE o.type IN ('P','FN','IF','TF','AF') ORDER BY o.name",
            {"limit": self._max_objects},
        )

        objects: list[DiscoveredObject] = []
        for r in tables:
            objects.append(
                DiscoveredObject(
                    schema_name=r["schema_name"], name=r["name"], kind="table",
                    row_estimate=r.get("row_estimate"), size_bytes=r.get("size_bytes"),
                )
            )
        for r in views:
            objects.append(DiscoveredObject(schema_name=r["schema_name"], name=r["name"], kind="view"))
        for r in indexes:
            objects.append(
                DiscoveredObject(
                    schema_name=r["schema_name"], name=r["name"], kind="index",
                    properties={
                        "type": r.get("index_type"),
                        "table": r.get("table_name"),
                        "user_seeks": r.get("user_seeks"),
                        "user_scans": r.get("user_scans"),
                        "user_lookups": r.get("user_lookups"),
                        "user_updates": r.get("user_updates"),
                    },
                )
            )
        for r in routines:
            objects.append(
                DiscoveredObject(schema_name=r["schema_name"], name=r["name"], kind=r["kind"])
            )
        return objects
