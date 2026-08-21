from __future__ import annotations

import pytest
from pydantic import ValidationError

from inumi.common.models.failures import FailureCode, InumiError
from inumi.common.models.target import DatabaseTarget, Environment


def test_resolves_unambiguous_production_target(target_validator):
    target = DatabaseTarget(environment=Environment.PRODUCTION, database="CoreBanking")
    resolved = target_validator.validate(target, ["environment", "instance", "database"])
    assert resolved.inventory_entry.id == "corebanking-prd-01"


def test_missing_required_field_is_invalid_target(target_validator):
    target = DatabaseTarget(environment=Environment.PRODUCTION, database="CoreBanking")
    with pytest.raises(InumiError) as exc:
        target_validator.validate(target, ["environment", "instance", "database", "session_id"])
    assert exc.value.code == FailureCode.INVALID_TARGET
    assert "session_id" in exc.value.detail


def test_unknown_database_is_invalid_target(target_validator):
    target = DatabaseTarget(environment=Environment.PRODUCTION, database="DoesNotExist")
    with pytest.raises(InumiError) as exc:
        target_validator.validate(target, ["environment", "instance", "database"])
    assert exc.value.code == FailureCode.INVALID_TARGET


def test_cannot_cross_environments_implicitly(target_validator):
    # A dev-only instance name must never resolve when environment=production.
    target = DatabaseTarget(environment=Environment.PRODUCTION, instance="sqlserver-dev-01")
    with pytest.raises(InumiError):
        target_validator.validate(target, ["environment", "instance"])


def test_llm_cannot_supply_arbitrary_connection_target():
    # DatabaseTarget is `extra="forbid"` — a free-text host/connection string
    # field is rejected by the pydantic model itself before it ever reaches
    # target validation.
    with pytest.raises(ValidationError):
        DatabaseTarget(
            environment=Environment.PRODUCTION,
            database="CoreBanking",
            connection_string="Server=evil;Trusted_Connection=True;",
        )
