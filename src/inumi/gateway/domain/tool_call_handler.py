"""The Gateway pipeline (spec's central diagram, §1/§74).

This is the ONLY path from an Agent tool call to an actual database
operation. Every stage below is mandatory and none can be skipped by
anything the Agent, the LLM, or a chat message claims:

  Tool Registry -> Argument Validation -> Target Validation -> Authorization
  -> Rate Limiting -> Policy Engine -> Risk Engine -> Approval Engine
  -> Execution -> Data Minimization -> Audit

If a required approval doesn't exist yet, execution stops and an
`APPROVAL_REQUIRED` response is returned instead of ever reaching the
Execution Service.
"""

from __future__ import annotations

import time

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from inumi.common.ids import new_id
from inumi.common.models.failures import FailureCode, InumiError
from inumi.common.models.identity import VerifiedIdentity
from inumi.common.models.target import DatabaseTarget
from inumi.common.models.tool import ToolCallRequest, ToolCallResponse, ToolCallStatus
from inumi.gateway.domain.approval import ApprovalContext, ApprovalDecision, ApprovalEngine
from inumi.gateway.domain.audit import AuditLog
from inumi.gateway.domain.authorization import authorize
from inumi.gateway.domain.data_policy import DataMinimizer
from inumi.gateway.domain.inventory import DatabaseInventory
from inumi.gateway.domain.policy_engine import PolicyDecision, PolicyEngine
from inumi.gateway.domain.rate_limiter import RateLimiter
from inumi.gateway.domain.risk_engine import RiskEngine
from inumi.gateway.domain.target_validation import TargetValidator
from inumi.gateway.domain.tool_catalog import ARGUMENT_MODELS
from inumi.gateway.domain.tool_registry import ToolRegistry
from inumi.gateway.infrastructure.execution_client import ExecutionClient
from inumi.common.models.execution import ExecutionRequest


def _enrich_target(target: DatabaseTarget, args_dict: dict) -> DatabaseTarget:
    """Fills object/session/query-level target fields from validated
    arguments when the caller only supplied them there — a single source of
    truth (the validated arguments model) rather than requiring the Agent to
    duplicate values across `target` and `arguments`."""
    updates = {}
    if args_dict.get("schema_name") and not target.schema_name:
        updates["schema"] = args_dict["schema_name"]
    if args_dict.get("table") and not target.object_name:
        updates["object"] = args_dict["table"]
    if args_dict.get("session_id") and not target.session_id:
        updates["session_id"] = args_dict["session_id"]
    if args_dict.get("query_id") and not target.query_id:
        updates["query_id"] = args_dict["query_id"]
    if not updates:
        return target
    return target.model_copy(update=updates)


class ToolCallHandler:
    def __init__(
        self,
        *,
        tool_registry: ToolRegistry,
        inventory: DatabaseInventory,
        target_validator: TargetValidator,
        policy_engine: PolicyEngine,
        risk_engine: RiskEngine,
        rate_limiter: RateLimiter,
        data_minimizer: DataMinimizer,
        execution_client: ExecutionClient,
        session: AsyncSession,
        agent_version: str = "unknown",
        channel: str = "unknown",
        identity_provider_name: str = "unknown",
    ):
        self._tools = tool_registry
        self._inventory = inventory
        self._targets = target_validator
        self._policy = policy_engine
        self._risk = risk_engine
        self._rate_limiter = rate_limiter
        self._minimizer = data_minimizer
        self._execution = execution_client
        self._session = session
        self._approvals = ApprovalEngine(session)
        self._audit = AuditLog(session)
        self._agent_version = agent_version
        self._channel = channel
        self._identity_provider_name = identity_provider_name

    async def handle(
        self, identity: VerifiedIdentity, request: ToolCallRequest
    ) -> ToolCallResponse:
        start = time.monotonic()
        correlation = {
            "conversation_id": request.conversation_id,
            "request_id": request.request_id,
            "investigation_id": request.investigation_id or "",
        }
        try:
            response = await self._handle_inner(identity, request, correlation)
            return response
        except InumiError as exc:
            await self._audit.record(
                event_type="TOOL_CALL_DENIED",
                correlation_ids=correlation,
                actor_subject_id=identity.subject_id,
                identity_provider=self._identity_provider_name,
                channel=self._channel,
                agent_version=self._agent_version,
                tool_id=request.tool_id,
                target=request.target,
                arguments=request.arguments,
                error_code=exc.code.value,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            if exc.code in (
                FailureCode.UNAUTHORIZED,
                FailureCode.AUTHENTICATION_FAILED,
                FailureCode.APPROVAL_MISMATCH,
                FailureCode.APPROVAL_INVALID,
                FailureCode.SEPARATION_OF_DUTIES_VIOLATION,
            ):
                await self._audit.record_security_event(
                    event_type=exc.code.value,
                    actor_subject_id=identity.subject_id,
                    detail={"tool_id": request.tool_id, "detail": exc.detail},
                )
            return ToolCallResponse(
                status=ToolCallStatus.DENIED,
                failure_code=exc.code.value,
                message=exc.detail,
            )

    async def _handle_inner(
        self, identity: VerifiedIdentity, request: ToolCallRequest, correlation: dict
    ) -> ToolCallResponse:
        # 1. Tool Registry
        tool = self._tools.get(request.tool_id, request.tool_version)

        # 2. Argument validation — strict, schema-bound, no arbitrary fields
        args_model = ARGUMENT_MODELS.get(tool.tool_id)
        if args_model is None:
            raise InumiError(FailureCode.TOOL_NOT_FOUND, f"No argument schema for '{tool.tool_id}'.")
        try:
            validated_args = args_model.model_validate(request.arguments)
        except ValidationError as exc:
            raise InumiError(
                FailureCode.INVALID_ARGUMENTS, f"Invalid arguments for '{tool.tool_id}': {exc.errors()[:3]}"
            ) from exc
        args_dict = validated_args.model_dump(mode="json")

        # 3. Target parsing + enrichment + resolution against inventory
        try:
            raw_target = DatabaseTarget.model_validate(request.target)
        except ValidationError as exc:
            raise InumiError(FailureCode.INVALID_TARGET, f"Invalid target: {exc.errors()[:3]}") from exc
        target = _enrich_target(raw_target, args_dict)
        resolved = self._targets.validate(target, tool.required_target_scope)
        entry = resolved.inventory_entry

        # 4. Authorization (independent of policy; hard identity/role gate)
        authorize(identity, tool, entry)

        # 5. Rate limiting
        await self._rate_limiter.check(
            operation_type=tool.operation_type.value,
            requires_approval=tool.requires_approval,
            user_subject_id=identity.subject_id,
            conversation_id=request.conversation_id,
            tool_id=tool.tool_id,
            database_id=entry.id,
            environment=entry.environment.value,
        )

        # 6. Policy Engine
        role = identity.highest_role()
        evaluation = self._policy.evaluate(
            environment=entry.environment,
            tool=tool,
            role=role,
            inventory_entry=entry,
            change_id=request.change_id,
        )
        if evaluation.decision == PolicyDecision.DENY:
            raise InumiError(
                FailureCode.POLICY_DENIED, f"Policy denies '{tool.tool_id}' for role {role.value}."
            )
        if evaluation.requires_change_ticket and not request.change_id:
            raise InumiError(
                FailureCode.CHANGE_TICKET_REQUIRED,
                f"'{tool.tool_id}' in {entry.environment.value} requires an approved change ticket.",
            )

        # 7. Risk Engine
        risk = self._risk.assess(tool=tool, environment=entry.environment, inventory_entry=entry)

        # 8. Approval Engine
        needs_approval = evaluation.decision == PolicyDecision.REQUIRES_APPROVAL
        approval_ctx = ApprovalContext(
            request_id=request.request_id,
            actor=identity,
            tool=tool,
            target=target,
            normalized_arguments=args_dict,
            environment=entry.environment.value,
            database_id=entry.id,
            risk=risk,
        )
        if needs_approval:
            if not request.approval_id:
                record = await self._approvals.create(
                    approval_ctx, requires_dual_approval=evaluation.requires_dual_approval
                )
                await self._audit.record(
                    event_type="APPROVAL_REQUESTED",
                    correlation_ids=correlation,
                    actor_subject_id=identity.subject_id,
                    identity_provider=self._identity_provider_name,
                    channel=self._channel,
                    agent_version=self._agent_version,
                    tool_id=tool.tool_id,
                    tool_version=tool.version,
                    target=target.model_dump(mode="json"),
                    arguments=args_dict,
                    policy_decision=evaluation.decision.value,
                    risk=risk.model_dump(mode="json"),
                    approval_id=record.approval_id,
                )
                return ToolCallResponse(
                    status=ToolCallStatus.APPROVAL_REQUIRED,
                    approval_id=record.approval_id,
                    message="This action requires DBA approval before it will run.",
                    risk=risk.model_dump(mode="json"),
                    policy_decision=evaluation.decision.value,
                )
            # Approval must have been granted for THIS EXACT action already.
            await self._approvals.verify_for_execution(
                approval_id=request.approval_id,
                expected_action_hash=approval_ctx.action_hash(),
            )

        # 9. Execution (only reachable once ALLOW, or REQUIRES_APPROVAL + verified)
        execution_id = new_id("exec")
        exec_request = ExecutionRequest(
            execution_id=execution_id,
            tool_id=tool.tool_id,
            tool_version=tool.version,
            platform=entry.platform,
            database_id=entry.id,
            instance=entry.instance,
            database=entry.database_name,
            schema_name=target.schema_name,
            object_name=target.object_name,
            session_id=target.session_id,
            query_id=target.query_id,
            arguments={
                **args_dict,
                **{
                    k: v
                    for k, v in {
                        "schema_name": target.schema_name,
                        "table": target.object_name,
                        "session_id": target.session_id,
                        "query_id": target.query_id,
                    }.items()
                    if v is not None
                },
            },
            max_execution_time=tool.max_execution_time,
            max_result_rows=tool.max_result_rows,
        )
        result = await self._execution.execute(exec_request)

        if not result.success:
            raise InumiError(
                FailureCode(result.error_code) if result.error_code else FailureCode.EXECUTION_FAILED,
                result.error_detail or "Execution failed.",
            )

        # 10. Data Minimization (applied here, once, before anything reaches the Agent)
        masked_rows, masked_fields, truncated = self._minimizer.apply(
            result.rows, max_rows=tool.max_result_rows
        )

        # 11. Audit (success)
        await self._audit.record(
            event_type="TOOL_CALL_EXECUTED",
            correlation_ids=correlation,
            actor_subject_id=identity.subject_id,
            identity_provider=self._identity_provider_name,
            channel=self._channel,
            agent_version=self._agent_version,
            tool_id=tool.tool_id,
            tool_version=tool.version,
            target=target.model_dump(mode="json"),
            arguments=args_dict,
            policy_decision=evaluation.decision.value,
            risk=risk.model_dump(mode="json"),
            approval_id=request.approval_id,
            execution_result="SUCCESS",
        )

        return ToolCallResponse(
            status=ToolCallStatus.EXECUTED,
            execution_id=execution_id,
            result={
                "columns": result.columns,
                "rows": masked_rows,
                "row_count": len(masked_rows),
                "truncated": truncated or result.truncated,
                "masked_fields": masked_fields,
                "affected": result.affected,
            },
            risk=risk.model_dump(mode="json"),
            policy_decision=evaluation.decision.value,
            message="Completed.",
        )
