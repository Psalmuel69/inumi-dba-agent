"""Agent orchestrator (spec §6, §7, §53, §54).

Ties the LLM's *proposals* to the Gateway's *decisions*. This module never
decides authorization, policy, risk, or approval itself — it only ever
forwards a `ToolCallRequest` and relays back exactly what the Gateway
decided. The investigation loop (spec §7's
understand -> identify target -> investigate -> collect evidence ->
correlate -> diagnose -> recommend -> assess action -> approve -> execute ->
verify -> report) lives here, bounded to a small number of steps so a
confused model can't loop forever.
"""

from __future__ import annotations

from inumi.agent.context_manager import ContextManager, ConversationState, PendingApproval
from inumi.agent.llm.base import LLMProvider
from inumi.agent.llm.registry import LLMRegistry
from inumi.agent.planner.actions import (
    AskClarification,
    Conclude,
    ProposeToolCall,
    RecordObservation,
)
from inumi.agent.playbooks.library import PLAYBOOKS, get_playbook, match_playbook
from inumi.agent.reply import AgentReply, ApprovalCard
from inumi.agent.tool_client import ToolClient
from inumi.common.ids import new_id
from inumi.common.models.tool import ToolCallRequest, ToolCallStatus
from inumi.common.observability import get_logger

logger = get_logger(__name__)

_MAX_INVESTIGATION_TURNS = 6

# A DENIED response whose failure_code means "the proposed call was shaped
# wrong" (not "this is not allowed") — the LLM can plausibly fix it given
# the specific reason, verified live: a real model that omitted a target
# field self-corrected immediately once the Gateway's exact error was fed
# back as an observation. Anything else (UNAUTHORIZED, POLICY_DENIED,
# TOOL_NOT_AVAILABLE, an approval-state problem, RATE_LIMITED, ...) is a
# permissions/policy fact no retry with different arguments changes, so
# those still end the turn immediately rather than burn the turn budget
# (and, with a real provider, API quota) retrying something that can only
# ever fail the same way.
_SELF_CORRECTABLE_DENIAL_CODES = {"INVALID_ARGUMENTS", "INVALID_TARGET", "TOOL_NOT_FOUND"}

_HELP_TEXT = (
    "I'm Inumi, your AI DBA assistant. I can investigate database health, "
    "performance, blocking, deadlocks, replication, backups, and more, and — "
    "with your role's approval where required — take controlled remediation "
    "actions. Try: \"Why is CoreBanking slow?\" or \"Check blocking on "
    "CoreBanking production.\"\n\nCommands: /help, /status, /approve <id>, "
    "/reject <id>, /models, /model <provider> <model>, /servers, /catalog <id>, "
    "/discover, /playbooks"
)


class AgentOrchestrator:
    def __init__(
        self, llm_registry: LLMRegistry, tool_client: ToolClient, context: ContextManager
    ):
        self._llm_registry = llm_registry
        self._tool_client = tool_client
        self._context = context

    def _llm_for(self, state: ConversationState) -> LLMProvider:
        return self._llm_registry.for_conversation(
            provider=state.llm_provider, model=state.llm_model
        )

    async def _list_servers_cached(self) -> list[dict]:
        try:
            return await self._tool_client.list_servers()
        except Exception:  # noqa: BLE001
            return []

    async def _known_database_names(self) -> list[str]:
        """Every discovered database name — helps the planner resolve
        "check CoreBanking" to a real target. Server ids/aliases are NOT
        included (they're resolved separately, via `instance_hint`)."""
        names: list[str] = []
        for s in await self._list_servers_cached():
            names.extend((s.get("catalog") or {}).get("databases", []))
        return [n for n in dict.fromkeys(names) if n]

    async def _known_server_hints(self) -> list[str]:
        """Every registered server id + alias — lets the planner recognize
        "on postgres-local" and narrow an otherwise-ambiguous target. The
        Gateway still independently re-resolves and validates whatever
        comes back; this only saves the DBA a disambiguation round-trip."""
        hints: list[str] = []
        for s in await self._list_servers_cached():
            hints.append(s["id"])
            hints.extend(s.get("aliases") or [])
        return [h for h in dict.fromkeys(hints) if h]

    async def handle_message(
        self,
        *,
        channel: str,
        channel_account_id: str,
        conversation_id: str,
        channel_thread_id: str,
        message: str,
    ) -> AgentReply:
        state = self._context.get_or_create(
            conversation_id, channel, channel_thread_id, channel_account_id
        )
        self._context.touch(state)

        command_reply = await self._handle_command_if_any(state, message, channel, channel_account_id)
        if command_reply is not None:
            return command_reply

        llm = self._llm_for(state)
        intent = await llm.extract_intent(
            message,
            known_database_names=await self._known_database_names(),
            known_server_hints=await self._known_server_hints(),
        )
        if intent.is_greeting_or_chitchat:
            return AgentReply(text=_HELP_TEXT)
        if not intent.is_dba_task:
            return AgentReply(
                text=(
                    "I can help with database health, performance, and operational "
                    "investigations. Could you tell me more about what you'd like me "
                    "to check?"
                ),
                status="clarification",
            )

        if state.investigation is None or state.investigation.status == "CONCLUDED":
            investigation = self._context.start_investigation(state, intent.problem_summary)
            # Deterministic, zero-LLM-call keyword match against a small
            # library of known scenarios (slow queries, high CPU, blocking,
            # ...) — see agent.playbooks.library for the rationale. None
            # means no known scenario matched; the loop below falls back to
            # the original fully-freeform behavior, unchanged.
            playbook = match_playbook(intent.problem_summary or message)
            if playbook is not None:
                investigation.playbook_id = playbook.playbook_id
            if intent.environment_hint:
                state.database_context["environment"] = intent.environment_hint
            if intent.database_hint:
                state.database_context["database"] = intent.database_hint
            if intent.instance_hint:
                state.database_context["instance"] = intent.instance_hint
        else:
            investigation = state.investigation

        if "environment" not in state.database_context:
            return AgentReply(
                text=(
                    "Which environment should I investigate — development, uat, or "
                    "production? For production targets I won't guess."
                ),
                status="clarification",
            )

        available = await self._tool_client.available_tools(channel, channel_account_id)
        available_ids = [t.tool_id for t in available]
        # Each tool's *actual* required arguments (its real Pydantic schema,
        # already alias-correct — e.g. "schema"/"table", not "schema_name"/
        # "table_name") — verified live: without this, the LLM has nothing
        # but the tool_id string to go on and reliably guesses wrong for any
        # schema/table-scoped write tool.
        tool_requirements = {
            t.tool_id: reqs
            for t in available
            if (reqs := t.argument_schema.get("required", []))
        }

        return await self._run_investigation_loop(
            state, investigation, available_ids, channel, channel_account_id, llm, tool_requirements
        )

    async def _run_investigation_loop(
        self,
        state: ConversationState,
        investigation,
        available_ids: list[str],
        channel: str,
        channel_account_id: str,
        llm: LLMProvider,
        tool_requirements: dict[str, list[str]] | None = None,
    ) -> AgentReply:
        while investigation.turn_count < _MAX_INVESTIGATION_TURNS:
            step_action = self._next_playbook_action(investigation, available_ids)
            if step_action is not None:
                # Deterministic step from a matched playbook — propose it
                # directly, skipping the LLM call entirely for this turn.
                # This is the actual point of a playbook: for a *known*
                # scenario, the sequence of diagnostics to run is already
                # decided, so there's nothing for the LLM to figure out here
                # — asking it anyway would only add latency and a chance of
                # a malformed completion for a call whose shape was never in
                # question. The LLM still gets one full turn at the end (once
                # playbook_step exhausts the step list, below falls through
                # to the normal decide_next_action call) to interpret
                # everything gathered and conclude.
                investigation.turn_count += 1
                reply = await self._submit_and_relay(
                    state, investigation, step_action, channel, channel_account_id
                )
                if reply is not None:
                    return reply
                continue  # executed (or failed-but-logged) — advance to the next step

            action = await llm.decide_next_action(
                problem_statement=self._problem_statement_for_llm(investigation),
                available_tool_ids=available_ids,
                transcript=investigation.transcript,
                turn_count=investigation.turn_count,
                tool_requirements=tool_requirements,
            )
            investigation.turn_count += 1

            if isinstance(action, AskClarification):
                return AgentReply(
                    text=action.question, status="clarification", investigation_id=investigation.investigation_id
                )

            if isinstance(action, RecordObservation):
                investigation.evidence.append(action.text)
                continue

            if isinstance(action, ProposeToolCall):
                reply = await self._submit_and_relay(
                    state, investigation, action, channel, channel_account_id
                )
                if reply is not None:
                    return reply
                continue  # executed successfully — loop for the next step

            if isinstance(action, Conclude):
                investigation.status = "CONCLUDED"
                if action.likely_root_cause:
                    investigation.findings.append(action.likely_root_cause)
                if action.recommendation:
                    investigation.recommendations.append(action.recommendation)
                return AgentReply(
                    text=self._format_report(investigation, action),
                    status="ok",
                    investigation_id=investigation.investigation_id,
                )

        investigation.status = "CONCLUDED"
        return AgentReply(
            text="I've run several diagnostic steps without reaching a confirmed root "
            "cause. Here's what I found:\n" + "\n".join(f"- {e}" for e in investigation.evidence),
            investigation_id=investigation.investigation_id,
        )

    def _next_playbook_action(self, investigation, available_ids: list[str]) -> ProposeToolCall | None:
        """The next step of this investigation's matched playbook, as a
        ready-to-submit ProposeToolCall — or None if there's no active
        playbook, or its steps are exhausted (falls back to the LLM either
        way). `investigation.playbook_step` always advances, even for a
        step that turns out to be unavailable — a fixed-argument step would
        fail the same way every time, so retrying it is never useful, and
        an unavailable tool is skipped silently (no turn spent, no Gateway
        round-trip) rather than surfaced as a denial for something the DBA
        never asked for by name."""
        playbook = get_playbook(investigation.playbook_id)
        if playbook is None:
            return None
        while investigation.playbook_step < len(playbook.steps):
            step = playbook.steps[investigation.playbook_step]
            investigation.playbook_step += 1
            if step.tool_id not in available_ids:
                continue
            return ProposeToolCall(
                tool_id=step.tool_id,
                arguments=dict(step.arguments),
                target={},
                reason=f"[{playbook.name} playbook] {step.purpose}",
            )
        return None

    @staticmethod
    def _problem_statement_for_llm(investigation) -> str:
        """The problem text handed to decide_next_action — unchanged for a
        freeform investigation. Once a matched playbook's steps are all
        used up, append its conclusion guidance so the one LLM call that
        follows (interpreting everything the playbook gathered) knows what
        "done" looks like for this specific scenario, and is nudged to
        conclude now rather than keep investigating freeform on top of it."""
        playbook = get_playbook(investigation.playbook_id)
        if playbook is None or investigation.playbook_step < len(playbook.steps):
            return investigation.problem
        return (
            f"{investigation.problem}\n\nYou just followed the '{playbook.name}' "
            f"playbook — see the transcript for what was checked and found. "
            f"{playbook.conclusion_guidance} If the evidence gathered is enough "
            "to conclude, conclude now rather than proposing further tool calls."
        )

    async def _submit_and_relay(
        self, state: ConversationState, investigation, action: ProposeToolCall, channel: str, channel_account_id: str
    ) -> AgentReply | None:
        request = ToolCallRequest(
            tool_id=action.tool_id,
            arguments=action.arguments,
            target={**state.database_context, **action.target},
            reason=action.reason,
            conversation_id=state.conversation_id,
            investigation_id=investigation.investigation_id,
            request_id=new_id("req"),
            channel=channel,
            channel_account_id=channel_account_id,
        )
        try:
            response = await self._tool_client.submit(request)
        except Exception as exc:  # noqa: BLE001 — a network-level failure reaching the
            # Gateway (verified live: httpcore.ReadTimeout when the database itself was
            # overloaded) — distinct from ToolCallStatus.FAILED below, which means the
            # Gateway/Execution pipeline DID respond with a structured "this call
            # failed" decision. Here we never got a response to relay at all, and
            # previously nothing caught that — it surfaced as an unhandled 500 instead
            # of a message. Ends the turn immediately rather than burning the rest of
            # the turn budget on further calls to the same likely-still-overloaded
            # target (see _SUBMIT_TIMEOUT_SECONDS for why this is bounded quickly).
            logger.warning(
                "tool_call_submit_failed", tool_id=action.tool_id, reason=action.reason, error=str(exc)
            )
            investigation.transcript.append(
                {"tool_id": action.tool_id, "reason": action.reason, "result": {"error": str(exc)}}
            )
            investigation.evidence.append(f"{action.tool_id} did not respond in time: {exc}")
            return AgentReply(
                text=(
                    f"I couldn't get a response for {action.tool_id} in time — the "
                    "database or Gateway may be under heavy load right now, which "
                    "could itself be relevant to what you're investigating. Try "
                    "again in a moment."
                ),
                status="error",
                investigation_id=investigation.investigation_id,
            )

        if response.status == ToolCallStatus.APPROVAL_REQUIRED:
            state.pending_approval = PendingApproval(
                approval_id=response.approval_id,
                tool_id=action.tool_id,
                summary=action.reason,
                request=request.model_dump(mode="json"),
            )
            risk = response.risk or {}
            card = ApprovalCard(
                approval_id=response.approval_id,
                tool_id=action.tool_id,
                target_summary=str(request.target),
                reason=action.reason,
                risk_level=risk.get("risk_level", "UNKNOWN"),
                blast_radius=risk.get("blast_radius", "UNKNOWN"),
            )
            return AgentReply(
                text=(
                    f"Recommended action: {action.tool_id} — {action.reason}\n"
                    f"Risk: {card.risk_level}\n\nThis requires DBA approval before I run it."
                ),
                status="approval_required",
                approval_card=card,
                investigation_id=investigation.investigation_id,
            )

        if response.status == ToolCallStatus.DENIED:
            correctable = (
                response.failure_code in _SELF_CORRECTABLE_DENIAL_CODES
                and investigation.turn_count < _MAX_INVESTIGATION_TURNS
            )
            if correctable:
                # Feed the exact rejection back as an observation and let
                # the loop continue — the LLM gets a concrete next chance to
                # fix the specific problem instead of the whole turn ending
                # on a malformed-but-fixable call.
                investigation.transcript.append(
                    {
                        "tool_id": action.tool_id,
                        "reason": action.reason,
                        "result": {"error": response.message, "failure_code": response.failure_code},
                    }
                )
                investigation.evidence.append(
                    f"{action.tool_id} was rejected ({response.failure_code}): {response.message}"
                )
                return None
            return AgentReply(
                text=f"I can't do that: {response.message}",
                status="denied",
                investigation_id=investigation.investigation_id,
            )

        if response.status == ToolCallStatus.FAILED:
            # An adapter-level failure (e.g. a diagnostic not implemented
            # for this engine, a transient connection error) — distinct
            # from DENIED (a policy fact) and previously unhandled here,
            # which meant it fell through to the "EXECUTED" branch below and
            # got logged as if the call had actually succeeded. That's a
            # real correctness gap a playbook makes more likely to surface
            # (it proactively calls diagnostics like get_replication_status
            # that a given engine/topology may not implement) — record it
            # plainly as a failed step and keep going; a single failed
            # diagnostic shouldn't abort the rest of the investigation.
            investigation.transcript.append(
                {"tool_id": action.tool_id, "reason": action.reason, "result": {"error": response.message}}
            )
            investigation.evidence.append(f"{action.tool_id} failed: {response.message}")
            return None

        # EXECUTED
        investigation.transcript.append(
            {"tool_id": action.tool_id, "reason": action.reason, "result": response.result or {}}
        )
        investigation.evidence.append(f"{action.tool_id}: {response.message}")
        investigation.actions.append({"tool_id": action.tool_id, "result": response.result})
        return None

    async def handle_approval_decision(
        self, *, conversation_id: str, decision: str, channel: str, channel_account_id: str
    ) -> AgentReply:
        state = self._context.get_or_create(conversation_id, channel, "", channel_account_id)
        pending = state.pending_approval
        if pending is None:
            return AgentReply(text="There is no pending approval on this conversation.", status="error")

        if decision == "reject":
            await self._tool_client.reject(pending.approval_id, channel, channel_account_id)
            state.pending_approval = None
            return AgentReply(text="Understood — action rejected and will not run.", status="ok")

        result = await self._tool_client.approve(pending.approval_id, channel, channel_account_id)
        if result.get("status") == "ERROR":
            return AgentReply(text=f"Approval failed: {result.get('detail')}", status="error")
        if result.get("status") == "AWAITING_SECOND_APPROVAL":
            return AgentReply(text="Recorded — this critical action also needs a second approver.", status="ok")

        # Fully approved — resubmit the exact original request with the approval_id.
        request = ToolCallRequest.model_validate({**pending.request, "approval_id": pending.approval_id})
        state.pending_approval = None  # cleared regardless — the Gateway already
        # recorded the approval; re-approving on a resubmit failure isn't meaningful.
        try:
            response = await self._tool_client.submit(request)
        except Exception as exc:  # noqa: BLE001 — same network-level-failure case as
            # _submit_and_relay above, but here the approval was already granted
            # server-side before this call — tell the DBA plainly what to check
            # rather than leaving them wondering whether the approved action ran.
            logger.warning(
                "approved_action_resubmit_failed", approval_id=pending.approval_id, error=str(exc)
            )
            return AgentReply(
                text=(
                    f"Your approval was recorded, but I couldn't confirm {pending.tool_id} "
                    f"executed — the Gateway didn't respond in time. Check the audit trail "
                    f"for approval_id {pending.approval_id} before retrying (see "
                    'OPERATIONS.md\'s "a DBA reports \'I approved it but nothing happened\'" runbook).'
                ),
                status="error",
            )

        investigation = state.investigation
        if response.status == ToolCallStatus.EXECUTED:
            if investigation is not None:
                investigation.actions.append({"tool_id": pending.tool_id, "result": response.result})
            return AgentReply(
                text=(
                    f"Action approved.\n\n{pending.tool_id} completed successfully.\n\n"
                    f"Result: {response.result}\n\nIncident status: MITIGATED."
                ),
                status="ok",
                investigation_id=investigation.investigation_id if investigation else None,
            )
        return AgentReply(
            text=f"Approved, but execution did not complete: {response.message}", status="error"
        )

    async def _handle_command_if_any(
        self, state: ConversationState, message: str, channel: str, channel_account_id: str
    ) -> AgentReply | None:
        stripped = message.strip()
        if stripped in ("/help",):
            return AgentReply(text=_HELP_TEXT)
        if stripped == "/status":
            inv = state.investigation
            if inv is None:
                return AgentReply(text="No active investigation on this conversation.")
            playbook = get_playbook(inv.playbook_id)
            playbook_note = (
                f" Following the '{playbook.name}' playbook (step "
                f"{min(inv.playbook_step, len(playbook.steps))}/{len(playbook.steps)})."
                if playbook is not None
                else ""
            )
            return AgentReply(
                text=f"Investigation {inv.investigation_id}: {inv.status}."
                f"{playbook_note} {len(inv.evidence)} observations so far.",
                investigation_id=inv.investigation_id,
            )
        if stripped == "/playbooks":
            return self._handle_playbooks_command()
        if stripped.startswith("/approve "):
            approval_id = stripped.split(" ", 1)[1].strip()
            if state.pending_approval and state.pending_approval.approval_id != approval_id:
                return AgentReply(text="That approval id doesn't match the pending action on this conversation.", status="error")
            return await self.handle_approval_decision(
                conversation_id=state.conversation_id, decision="approve", channel=channel, channel_account_id=channel_account_id
            )
        if stripped.startswith("/reject "):
            approval_id = stripped.split(" ", 1)[1].strip()
            if state.pending_approval and state.pending_approval.approval_id != approval_id:
                return AgentReply(text="That approval id doesn't match the pending action on this conversation.", status="error")
            return await self.handle_approval_decision(
                conversation_id=state.conversation_id, decision="reject", channel=channel, channel_account_id=channel_account_id
            )
        if stripped in ("/models", "/model"):
            return await self._handle_model_command(state, stripped)
        if stripped.startswith("/model "):
            return await self._handle_model_command(state, stripped)
        if stripped == "/servers":
            return await self._handle_servers_command()
        if stripped == "/catalog" or stripped.startswith("/catalog "):
            return await self._handle_catalog_command(stripped)
        if stripped == "/discover" or stripped.startswith("/discover "):
            return await self._handle_discover_command(stripped, channel, channel_account_id)
        return None

    def _handle_playbooks_command(self) -> AgentReply:
        lines = [f"- {p.name}: {p.description}" for p in PLAYBOOKS]
        return AgentReply(
            text="I automatically follow one of these fixed diagnostic sequences "
            "when your message matches its scenario, instead of investigating "
            "fully freeform:\n" + "\n".join(lines)
        )

    async def _handle_servers_command(self) -> AgentReply:
        servers = await self._tool_client.list_servers()
        if not servers:
            return AgentReply(text="No servers are registered.")
        lines = []
        for s in servers:
            cat = s.get("catalog")
            summary = (
                f"{cat['database_count']} databases, discovered "
                f"{(cat['discovered_at'] or '')[:16]}"
                if cat
                else "not yet discovered — run /discover"
            )
            lines.append(
                f"- {s['id']}  [{s['environment']}/{s['platform']}, {s['criticality']}]  {summary}"
            )
        return AgentReply(text="Registered servers:\n" + "\n".join(lines))

    async def _handle_catalog_command(self, stripped: str) -> AgentReply:
        parts = stripped.split(maxsplit=1)
        if len(parts) < 2:
            return AgentReply(text="Usage: /catalog <server-id>  (see /servers)")
        data = await self._tool_client.get_server_catalog(parts[1].strip())
        cat = (data or {}).get("catalog")
        if not cat:
            return AgentReply(
                text=f"No catalog for '{parts[1].strip()}' yet — run /discover {parts[1].strip()}"
            )
        lines = [f"{data['server']['id']} — {cat['engine_edition']} {cat['engine_version']}"]
        for db in cat["databases"][:40]:
            kinds: dict[str, int] = {}
            for o in db["objects"]:
                kinds[o["kind"]] = kinds.get(o["kind"], 0) + 1
            size = f"{db['size_bytes'] / 1e6:.0f}MB" if db.get("size_bytes") else "?"
            exts = f", {len(db['extensions'])} extensions" if db.get("extensions") else ""
            lines.append(f"  {db['name']} ({db['state']}, {size}) — {dict(kinds)}{exts}")
        if cat.get("warnings"):
            lines.append(f"  warnings: {cat['warnings'][:3]}")
        return AgentReply(text="\n".join(lines))

    async def _handle_discover_command(
        self, stripped: str, channel: str, channel_account_id: str
    ) -> AgentReply:
        parts = stripped.split(maxsplit=1)
        server_id = parts[1].strip() if len(parts) > 1 else None
        result = await self._tool_client.refresh_catalog(channel, channel_account_id, server_id)
        if result.get("status") == "ERROR":
            return AgentReply(text=f"Discovery failed: {result.get('detail')}", status="error")
        return AgentReply(text=f"Discovery complete:\n{result}")

    async def _handle_model_command(self, state: ConversationState, stripped: str) -> AgentReply:
        registry = self._llm_registry
        parts = stripped.split()

        # `/models` — list what's available.
        if parts[0] == "/models":
            return AgentReply(text=await registry.describe_available())

        # `/model` — show the current selection.
        if len(parts) == 1:
            if state.llm_provider:
                current = state.llm_provider + (f" / {state.llm_model}" if state.llm_model else "")
                source = "this conversation"
            else:
                dp, dm = registry.default()
                current = dp + (f" / {dm}" if dm else " (provider default)")
                source = "deployment default"
            hint = "" if registry.selection_enabled() else " (switching is disabled here)"
            return AgentReply(
                text=f"Current model: {current} — {source}.{hint}\n"
                "Use `/model <provider> <model>` to switch, or `/models` to list options."
            )

        # `/model <provider> [<model>]` — switch.
        provider = parts[1].lower()
        model = parts[2] if len(parts) >= 3 else None
        error = registry.validate_selection(provider, model)
        if error:
            return AgentReply(text=error, status="error")
        if provider != "mock" and model is not None:
            available_models = await registry.list_models(provider)
            if available_models and model not in available_models:
                preview = ", ".join(available_models[:10])
                return AgentReply(
                    text=f"'{model}' isn't in {provider}'s available models. Options: {preview}",
                    status="error",
                )
        state.llm_provider = provider
        state.llm_model = model
        chosen = provider + (f" / {model}" if model else " (provider default)")
        return AgentReply(text=f"Model for this conversation set to {chosen}.")

    @staticmethod
    def _format_report(investigation, conclusion: Conclude) -> str:
        lines = []
        playbook = get_playbook(investigation.playbook_id)
        if playbook is not None:
            lines.append(f"Followed the '{playbook.name}' playbook.")
        lines.append(f"Summary: {conclusion.summary}")
        if investigation.evidence:
            lines.append("Evidence: " + "; ".join(investigation.evidence))
        if conclusion.likely_root_cause:
            prefix = {"confirmed": "Confirmed", "likely": "Likely", "unable_to_confirm": "Unable to confirm"}[
                conclusion.confidence
            ]
            lines.append(f"Root Cause ({prefix}): {conclusion.likely_root_cause}")
        if conclusion.recommendation:
            lines.append(f"Recommended Action: {conclusion.recommendation}")
        return "\n".join(lines)
