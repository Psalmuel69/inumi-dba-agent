"""Recall recent findings for a server before a new investigation starts —
the same "look up, degrade to nothing rather than fail hard" shape as
`DiscoveryOrchestrator.ensure_fresh`.
"""

from __future__ import annotations

from inumi.common.models.investigation import InvestigationMemoryEntry
from inumi.common.observability import get_logger
from inumi.gateway.domain.investigation_store import InvestigationStore

logger = get_logger(__name__)

# Only a concluded investigation's findings/recommendations are meaningful
# background for a new one — one still in progress (or awaiting
# clarification/verification) has nothing settled to recall yet.
_CONCLUDED_PREFIX = "CONCLUDED"


class InvestigationMemory:
    def __init__(self, store: InvestigationStore):
        self._store = store

    async def recall(
        self,
        server_id: str,
        *,
        exclude_investigation_id: str | None = None,
        limit: int = 3,
    ) -> list[InvestigationMemoryEntry]:
        if limit <= 0:
            return []
        try:
            # Over-fetch: the store's `limit` doesn't know about the
            # concluded-only filter applied below, so asking for exactly
            # `limit` rows could under-return if recent ones are still
            # in-progress. A small multiplier keeps this a single query
            # without adding a status filter to the store's generic
            # interface (which `find_similar`/Phase 5 doesn't want).
            records = await self._store.recent_for_server(
                server_id,
                limit=limit * 4,
                exclude_investigation_id=exclude_investigation_id,
            )
        except Exception as exc:  # noqa: BLE001 — a lookup failure must never block
            # a new investigation from starting; recall is an enhancement,
            # not a dependency.
            logger.warning(
                "investigation_memory_lookup_failed", server_id=server_id, error=str(exc)
            )
            return []
        return [
            InvestigationMemoryEntry(
                investigation_id=r.investigation_id,
                problem=r.problem,
                status=r.status,
                findings=r.findings,
                recommendations=r.recommendations,
                updated_at=r.updated_at,
            )
            for r in records
            if r.status.startswith(_CONCLUDED_PREFIX)
        ][:limit]
