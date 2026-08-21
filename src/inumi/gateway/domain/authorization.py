"""Authorization gate (spec §3, §5, §37).

This is independent of, and evaluated before, the Policy Engine's
fine-grained ALLOW/DENY/REQUIRES_APPROVAL decision. It answers three coarse
questions using ONLY the independently-verified identity — never anything
claimed in chat text or by the LLM:

  1. Is this person a member of the DBA team at all?
  2. Does their verified role even appear in this tool's allowed_roles?
  3. Does their verified role appear in this *database's* allowed_roles
     (spec §11 — e.g. CoreBanking production requires DBA_L2+)?
  4. Is this tool permitted in this target's environment at all?

A conversation switching from one database to another, or the passage of
time invalidating a cached role, must re-run this check — nothing here is
cached across a conversation turn (spec §37).
"""

from __future__ import annotations

from inumi.common.models.failures import FailureCode, InumiError
from inumi.common.models.identity import VerifiedIdentity
from inumi.common.models.tool import ToolDefinition
from inumi.gateway.domain.inventory import InventoryEntry


def authorize(
    identity: VerifiedIdentity,
    tool: ToolDefinition,
    inventory_entry: InventoryEntry,
) -> None:
    """Raises InumiError(UNAUTHORIZED) on any failure; returns None on success."""
    if not identity.is_dba():
        raise InumiError(
            FailureCode.UNAUTHORIZED,
            "This account is not a member of the DBA team.",
        )

    if not identity.mfa_satisfied:
        raise InumiError(
            FailureCode.AUTHENTICATION_FAILED,
            "Multi-factor authentication has not been satisfied for this session.",
        )

    user_roles = set(identity.dba_roles)

    if not (user_roles & set(tool.allowed_roles)):
        raise InumiError(
            FailureCode.UNAUTHORIZED,
            f"Your role does not permit use of '{tool.tool_id}'.",
        )

    if inventory_entry.allowed_roles and not (user_roles & set(inventory_entry.allowed_roles)):
        raise InumiError(
            FailureCode.UNAUTHORIZED,
            f"Your role does not permit access to database '{inventory_entry.id}'.",
        )

    if inventory_entry.environment not in tool.allowed_environments:
        raise InumiError(
            FailureCode.UNAUTHORIZED,
            f"'{tool.tool_id}' is not permitted in environment "
            f"'{inventory_entry.environment.value}'.",
        )
