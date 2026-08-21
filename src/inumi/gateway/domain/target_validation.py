"""Target validation (spec §10, §11).

Confirms a proposed target (a) carries every field a given tool requires,
and (b) resolves to exactly one real, active inventory entry. The LLM never
gets to supply a target that skips this step — every tool call goes through
`TargetValidator.validate` before policy/risk evaluation even begins.
"""

from __future__ import annotations

from dataclasses import dataclass

from inumi.common.models.failures import FailureCode, InumiError
from inumi.common.models.target import REQUIRED_FIELD_SETS, DatabaseTarget, RequiredTargetFields
from inumi.gateway.domain.inventory import AmbiguousTargetError, DatabaseInventory, InventoryEntry

# Maps the ToolDefinition.required_target_scope string labels (which mirror
# DatabaseTarget field names directly, per spec §10's worked examples) to the
# fields that must be non-empty on the target.
_FIELD_ALIASES = {
    "schema": "schema_name",
    "object": "object_name",
}


@dataclass(frozen=True)
class ResolvedTarget:
    target: DatabaseTarget
    inventory_entry: InventoryEntry


class TargetValidator:
    def __init__(self, inventory: DatabaseInventory):
        self._inventory = inventory

    def _check_required_fields(
        self, target: DatabaseTarget, entry: InventoryEntry, required_scope: list[str]
    ) -> None:
        # Fields the inventory itself can supply (instance/database/cluster)
        # don't have to be typed by the user — spec §10: "not every operation
        # requires every field" from the *caller*. Anything the inventory
        # can't know about (session_id, query_id, schema, object) must still
        # come from the actual request.
        effective = {
            "environment": target.environment.value,
            "instance": target.instance or entry.instance,
            "database": target.database or entry.database_name,
            "cluster": target.cluster or entry.cluster,
            "schema_name": target.schema_name,
            "object_name": target.object_name,
            "session_id": target.session_id,
            "query_id": target.query_id,
        }
        missing = []
        for field_name in required_scope:
            attr = _FIELD_ALIASES.get(field_name, field_name)
            if not effective.get(attr):
                missing.append(field_name)
        if missing:
            raise InumiError(
                FailureCode.INVALID_TARGET,
                f"Missing required target field(s) for this operation: {', '.join(missing)}.",
            )

    def validate(self, target: DatabaseTarget, required_scope: list[str]) -> ResolvedTarget:
        try:
            entry = self._inventory.resolve(target)
        except AmbiguousTargetError as exc:
            names = ", ".join(f"{c.id} ({c.environment.value})" for c in exc.candidates)
            raise InumiError(
                FailureCode.INVALID_TARGET,
                f"Multiple databases match this target — please specify which one: {names}.",
            ) from exc
        except LookupError as exc:
            raise InumiError(
                FailureCode.INVALID_TARGET,
                "No database in the inventory matches the given target.",
            ) from exc

        if entry.status != "active":
            raise InumiError(FailureCode.INVALID_TARGET, f"Database '{entry.id}' is not active.")

        self._check_required_fields(target, entry, required_scope)

        return ResolvedTarget(target=target, inventory_entry=entry)
