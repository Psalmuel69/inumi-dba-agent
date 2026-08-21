from __future__ import annotations

from inumi.common.models.risk import BlastRadius, ReasonCode, RiskLevel
from inumi.common.models.target import Environment
from inumi.gateway.domain.risk_engine import RiskEngine


def test_read_tool_on_critical_prod_db_is_low_risk(tool_registry, inventory):
    engine = RiskEngine()
    tool = tool_registry.get("database.get_health")
    entry = inventory.by_id("corebanking-prd-01")
    risk = engine.assess(tool=tool, environment=Environment.PRODUCTION, inventory_entry=entry)
    assert risk.risk_level == RiskLevel.LOW
    assert risk.blast_radius == BlastRadius.SINGLE_OBJECT
    assert ReasonCode.READ_OPERATION in risk.reason_codes


def test_kill_session_on_critical_prod_db_is_at_least_medium(tool_registry, inventory):
    engine = RiskEngine()
    tool = tool_registry.get("database.kill_session")
    entry = inventory.by_id("corebanking-prd-01")
    risk = engine.assess(tool=tool, environment=Environment.PRODUCTION, inventory_entry=entry)
    assert risk.risk_level in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)
    assert risk.blast_radius == BlastRadius.SINGLE_SESSION
    assert risk.reversible is False
    assert ReasonCode.CRITICAL_DATABASE in risk.reason_codes


def test_failover_is_always_critical(tool_registry, inventory):
    engine = RiskEngine()
    tool = tool_registry.get("database.failover")
    entry = inventory.by_id("sqlserver-dev-01")  # even a low-criticality dev db
    risk = engine.assess(tool=tool, environment=Environment.DEVELOPMENT, inventory_entry=entry)
    assert risk.risk_level == RiskLevel.CRITICAL
    assert risk.blast_radius == BlastRadius.CLUSTER


def test_risk_never_reported_below_tool_floor(tool_registry, inventory):
    engine = RiskEngine()
    tool = tool_registry.get("database.restart_instance")
    entry = inventory.by_id("sqlserver-dev-01")
    risk = engine.assess(tool=tool, environment=Environment.DEVELOPMENT, inventory_entry=entry)
    assert risk.risk_level == RiskLevel.CRITICAL  # tool floor is CRITICAL
