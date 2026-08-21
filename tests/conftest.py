from __future__ import annotations

import pytest
import pytest_asyncio

from inumi.common.config import Settings
from inumi.common.identity import MockIdentityProvider
from inumi.gateway.domain.inventory import DatabaseInventory
from inumi.gateway.domain.policy_engine import PolicyEngine
from inumi.gateway.domain.rate_limiter import InMemoryRateLimitBackend, RateLimiter
from inumi.gateway.domain.target_validation import TargetValidator
from inumi.gateway.domain.tool_registry import ToolRegistry
from inumi.gateway.infrastructure.db.session import Database


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def identity_provider() -> MockIdentityProvider:
    return MockIdentityProvider("config/identity.yaml")


@pytest.fixture
def inventory() -> DatabaseInventory:
    return DatabaseInventory("config/inventory.yaml")


@pytest.fixture
def target_validator(inventory: DatabaseInventory) -> TargetValidator:
    return TargetValidator(inventory)


@pytest.fixture
def policy_engine() -> PolicyEngine:
    return PolicyEngine("config/policy.yaml")


@pytest.fixture
def tool_registry(settings: Settings) -> ToolRegistry:
    return ToolRegistry(settings)


@pytest.fixture
def rate_limiter() -> RateLimiter:
    return RateLimiter("config/rate_limits.yaml", InMemoryRateLimitBackend())


@pytest_asyncio.fixture
async def db() -> Database:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()
