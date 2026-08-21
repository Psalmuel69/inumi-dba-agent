"""Rate limiting (spec §29).

Enforced by the Gateway before policy evaluation, at multiple keys
(user/conversation/tool/database/environment), with separate, much stricter
budgets for write and critical operations. Two backends satisfy the same
interface: an in-memory fixed-window counter for tests/local dev, and Redis
for multi-instance production deployments.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path

import yaml

from inumi.common.models.failures import FailureCode, InumiError


class RateLimitBackend(ABC):
    @abstractmethod
    async def increment_and_check(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        """Returns True if the call is within budget (and records it),
        False if the limit has been exceeded."""


class InMemoryRateLimitBackend(RateLimitBackend):
    def __init__(self) -> None:
        self._buckets: dict[str, tuple[int, float]] = {}  # key -> (count, window_start)

    async def increment_and_check(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.time()
        count, window_start = self._buckets.get(key, (0, now))
        if now - window_start >= window_seconds:
            count, window_start = 0, now
        count += 1
        self._buckets[key] = (count, window_start)
        return count <= limit


class RedisRateLimitBackend(RateLimitBackend):
    """Production backend. Uses a simple INCR + EXPIRE fixed window, which is
    sufficient given the coarse per-minute budgets configured here."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def increment_and_check(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds)
        count, _ = await pipe.execute()
        return int(count) <= limit


class RateLimiter:
    def __init__(self, config_path: str | Path, backend: RateLimitBackend):
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        self._limits = raw["rate_limits"]
        self._backend = backend

    def _tier_for(self, operation_type: str, requires_approval: bool) -> str:
        if operation_type == "PRIVILEGED" or requires_approval:
            return "critical"
        if operation_type == "WRITE":
            return "write"
        return "read"

    async def check(
        self,
        *,
        operation_type: str,
        requires_approval: bool,
        user_subject_id: str,
        conversation_id: str,
        tool_id: str,
        database_id: str,
        environment: str,
    ) -> None:
        tier = self._tier_for(operation_type, requires_approval)
        limits = self._limits[tier]
        checks = [
            (f"rl:{tier}:user:{user_subject_id}", limits["per_user_per_minute"]),
            (f"rl:{tier}:conv:{conversation_id}", limits["per_conversation_per_minute"]),
            (f"rl:{tier}:tool:{tool_id}", limits["per_tool_per_minute"]),
            (f"rl:{tier}:db:{database_id}", limits["per_database_per_minute"]),
            (f"rl:{tier}:env:{environment}", limits["per_environment_per_minute"]),
        ]
        for key, limit in checks:
            ok = await self._backend.increment_and_check(key, limit)
            if not ok:
                raise InumiError(
                    FailureCode.RATE_LIMITED,
                    "Rate limit exceeded — please slow down and try again shortly.",
                )
