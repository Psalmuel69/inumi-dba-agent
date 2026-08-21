"""Database inventory service (spec §11, §39).

The single source of truth for which database targets exist. The Agent
proposes a `DatabaseTarget` with human-supplied names ("CoreBanking",
"production"); this module is what turns that into a concrete, validated
inventory entry — or reports ambiguity / not-found rather than guessing.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from inumi.common.models.identity import DBARole
from inumi.common.models.target import DatabaseTarget, Environment, Platform


class InventoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    platform: Platform
    environment: Environment
    organization: str = ""
    instance: str
    cluster: str = ""
    database_name: str = ""
    region: str = ""
    criticality: str = "standard"
    classification: str = "internal"
    owner: str = ""
    support_team: str = ""
    maintenance_window: dict = {}
    allowed_roles: list[DBARole] = []
    status: str = "active"


class AmbiguousTargetError(Exception):
    def __init__(self, candidates: list[InventoryEntry]):
        self.candidates = candidates
        super().__init__(f"{len(candidates)} databases match the given target.")


class DatabaseInventory:
    def __init__(self, config_path: str | Path):
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        self._entries: list[InventoryEntry] = [
            InventoryEntry.model_validate(e) for e in raw.get("databases", [])
        ]

    def all(self) -> list[InventoryEntry]:
        return list(self._entries)

    def by_id(self, database_id: str) -> InventoryEntry | None:
        for e in self._entries:
            if e.id == database_id:
                return e
        return None

    def find_candidates(self, target: DatabaseTarget) -> list[InventoryEntry]:
        """Return every inventory entry consistent with the (possibly
        partial) target. Never widens beyond `environment` — cross-
        environment matches are never offered as candidates."""
        candidates = [e for e in self._entries if e.environment == target.environment]

        if target.platform is not None:
            candidates = [e for e in candidates if e.platform == target.platform]

        if target.instance:
            needle = target.instance.strip().lower()
            exact = [e for e in candidates if e.instance.lower() == needle or e.id.lower() == needle]
            if exact:
                candidates = exact
            else:
                candidates = [e for e in candidates if needle in e.instance.lower()]

        if target.cluster:
            needle = target.cluster.strip().lower()
            candidates = [e for e in candidates if needle in e.cluster.lower()]

        if target.database:
            needle = target.database.strip().lower()
            exact = [e for e in candidates if e.database_name.lower() == needle]
            candidates = exact if exact else [
                e for e in candidates if needle in e.database_name.lower()
            ]

        return [e for e in candidates if e.status == "active"]

    def resolve(self, target: DatabaseTarget) -> InventoryEntry:
        """Resolve to exactly one inventory entry, or raise.

        Raises `AmbiguousTargetError` (caller should ask the user to
        disambiguate, per spec §39) or `LookupError` if nothing matches.
        """
        candidates = self.find_candidates(target)
        if len(candidates) == 0:
            raise LookupError("No database in the inventory matches the given target.")
        if len(candidates) > 1:
            raise AmbiguousTargetError(candidates)
        return candidates[0]
