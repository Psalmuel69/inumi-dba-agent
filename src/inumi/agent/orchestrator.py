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

import json
import re

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
from inumi.common.server_reference import normalize_server_reference

logger = get_logger(__name__)

_MAX_INVESTIGATION_TURNS = 6

# How many record_observation actions in a row (never interrupted by a new
# tool call or a conclude) are tolerated before giving up on asking the LLM
# to conclude and using whatever evidence already exists instead. Verified
# live: a real model can get stuck restating the same finding as one
# observation after another rather than ever emitting action=conclude —
# reproduced on the replication playbook specifically, where the real
# answer ("no replica configured") was already clear after the very first
# observation, yet the model spent its remaining turns re-recording that
# same conclusion as evidence and hit the full turn cap without ever
# reaching the DBA. Small and deliberate: this is meant to catch a stuck
# pattern fast, not to second-guess a model that's still making real
# progress (any other action resets the count to 0).
_MAX_CONSECUTIVE_RECORD_OBSERVATIONS = 2

# Bounds a run of *consecutive* AskClarification turns independently of
# _MAX_INVESTIGATION_TURNS (see the loop's own comment for why they're
# counted separately) — an unresolved back-and-forth (environment, then
# server, then database, ...) still can't run forever across many separate
# requests, since turn_count alone never catches that.
_MAX_CLARIFICATION_TURNS = 4

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

# Same heuristic agent.llm.mock already uses to spot a table/database name in
# free text ("a CamelCase word is probably a schema object"). Used here to
# catch the *other* direction: a conclusion's free-text fields naming an
# object that was never actually seen anywhere in this investigation —
# verified live: a real conclusion named three such identifiers
# (AccountBalanceOutstandings, AccountBalances, TransactionPostingHistory_2)
# that don't exist in the database at all, instead of the real table names
# (Branch, production.location, ...) its own tool call had actually
# returned — likely primed by "CoreBanking"-style example names used
# throughout this file's own system prompts/help text.
_CAMEL_CASE_NAME_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")

# A bare answer to "which environment should I investigate" — matched
# word-boundary, case-insensitive, so it still finds "development" inside
# something like "development <@U0BOTID>" (a Slack mention appended after
# the actual answer) without needing an LLM call at all. See its one call
# site in handle_message for why this exists as a fast-path.
_ENVIRONMENT_ANSWER_RE = re.compile(r"\b(development|uat|production)\b", re.IGNORECASE)


def _ungrounded_identifiers(conclusion: Conclude, investigation) -> list[str]:
    """CamelCase-looking identifiers the conclusion's summary/root-cause/
    recommendation text claims, that don't appear anywhere this
    investigation's transcript, running evidence, or the DBA's own problem
    statement — the last one specifically so a term the DBA themselves used
    (e.g. "AlwaysOn") is never flagged just because it isn't a tool call's
    own output. This is a heuristic, not a proof of hallucination — it only
    ever causes one more self-correction turn (see the Conclude branch
    below), never blocks a conclusion from ever landing."""
    # Excludes this same check's own past rejection notes — they necessarily
    # quote the rejected name back (for a human reading the transcript), and
    # without this exclusion that quoting would "ground" the name for every
    # later attempt in the same investigation, defeating the whole check on
    # a second try. Caught by test_repeated_ungrounded_conclusions_fall_
    # through_to_the_safe_fallback before this ever shipped.
    real_transcript = [t for t in investigation.transcript if t.get("tool_id") != "internal.grounding_check"]
    real_evidence = [e for e in investigation.evidence if not e.startswith("(a draft conclusion naming")]
    haystack = " ".join(
        [json.dumps(real_transcript, default=str), investigation.problem, " ".join(real_evidence)]
    ).lower()
    claimed = " ".join(
        filter(None, [conclusion.summary, conclusion.likely_root_cause, conclusion.recommendation])
    )
    seen: set[str] = set()
    ungrounded: list[str] = []
    for match in _CAMEL_CASE_NAME_RE.finditer(claimed):
        name = match.group(0)
        if name in seen:
            continue
        seen.add(name)
        if name.lower() not in haystack:
            ungrounded.append(name)
    return ungrounded


def _affected_summary(result: dict | None) -> str:
    """A write tool's actual outcome (`execution.ExecutionResult.affected`
    — e.g. `{"terminated": False, "session_id": "13400"}` for kill_session)
    is real information the Gateway already returns, but the response's own
    `message` is a hardcoded "Completed." for every EXECUTED call
    regardless of what `affected` actually says (see
    `gateway.domain.tool_call_handler`) — a DBA reading only "Completed."
    has no way to tell a kill that returned `terminated: false` (nothing
    was actually there to kill — see the double kill_session call this
    surfaced live: a session already gone by the second attempt) from one
    that actually terminated something. Read tools never populate
    `affected` (they populate `rows`/`row_count` instead), so this is a
    no-op for them — only ever adds detail for a write."""
    affected = (result or {}).get("affected")
    if not affected:
        return ""
    return " (" + ", ".join(f"{k}={v}" for k, v in affected.items()) + ")"


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

        # Resuming an in-progress investigation — the DBA's reply is part of
        # THIS conversation, not a fresh, standalone utterance to classify
        # from scratch. Verified live (twice, two different clarification
        # kinds): a short reply like "development" (answering the hardcoded
        # environment gate) or a bare server name (answering a freeform
        # AskClarification the LLM itself asked) both got reclassified by
        # extract_intent as chitchat/non-DBA and silently dropped the whole
        # investigation. decide_next_action already has the full transcript
        # and knows exactly what it just asked, so hand it the raw reply
        # directly — via investigation.last_message, see
        # _problem_statement_for_llm — instead of ever risking that
        # misclassification again. The one thing still enforced here, not
        # left to the LLM: never guessing an environment (spec: "for
        # production targets I won't guess") — a bare answer to that
        # specific question is recognized deterministically; anything else
        # while it's still missing re-asks rather than guessing.
        if state.investigation is not None and state.investigation.status != "CONCLUDED":
            investigation = state.investigation
            if "environment" not in state.database_context:
                match = _ENVIRONMENT_ANSWER_RE.search(message)
                if match:
                    state.database_context["environment"] = match.group(1).lower()
                else:
                    return AgentReply(
                        text=(
                            "Which environment should I investigate — development, uat, "
                            "or production? For production targets I won't guess."
                        ),
                        status="clarification",
                    )
            investigation.last_message = message
            return await self._continue_investigation(state, investigation, channel, channel_account_id)

        llm = self._llm_for(state)
        intent = await llm.extract_intent(
            message,
            known_database_names=await self._known_database_names(),
            known_server_hints=await self._known_server_hints(),
        )
        if intent.meta_command and not (
            intent.meta_command in ("approve", "reject") and state.pending_approval is None
        ):
            # A free-text equivalent of one of the exact slash commands
            # below (spec: the DBA should never be bound to a fixed
            # message structure) — "list my servers" works exactly like
            # "/servers", "what playbooks do you have" like "/playbooks",
            # etc. Checked before the greeting/chitchat gate since these
            # are a distinct, actionable third category, never either of
            # those. The approve/reject exclusion above is deliberate —
            # verified live: "go ahead and terminate session 19860"
            # matched the same phrasing as reacting to a shown approval
            # card ("go ahead" -> approve), but there was no pending
            # approval to react to, and it dead-ended with "there is no
            # pending approval" instead of acting. extract_intent has no
            # visibility into whether one actually exists (it classifies
            # from the raw message alone), so that specific combination
            # falls through to the normal DBA-task path below instead —
            # a message naming a specific session/server/action is far
            # more likely a fresh instruction than a reaction to nothing.
            return await self._handle_meta_command(intent, state, channel, channel_account_id)
        if (
            intent.meta_command in ("approve", "reject")
            and state.pending_approval is None
            and not intent.is_dba_task
        ):
            # See above — falling through, so this must still be treated
            # as the actionable DBA task it obviously is, not dismissed by
            # the classifier's own (now-irrelevant) is_dba_task verdict.
            intent.is_dba_task = True
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

        # Reachable only for a brand-new investigation (None) or a
        # previously-concluded one starting fresh — an active one already
        # returned above, before ever reaching extract_intent. Falls back
        # to the raw message if problem_summary is blank — verified live:
        # reachable via the approve/reject-with-no-pending-approval
        # fallback above, where a real model classifying the message as
        # meta_command left problem_summary empty since nothing told it to
        # fill that in for that path too.
        investigation = self._context.start_investigation(state, intent.problem_summary or message)
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
            if not intent.environment_hint:
                # A specific, registered server was named but no
                # environment was — verified live: naming "postgres-local"
                # explicitly still triggered "which environment should I
                # investigate?" even though a registered server has
                # exactly one environment in config/servers.yaml. Asking
                # again for something already implied by the instance is
                # never necessary; only ever fills a gap, never overrides
                # an environment the DBA actually stated.
                auto_environment = await self._environment_for_instance(intent.instance_hint)
                if auto_environment:
                    state.database_context["environment"] = auto_environment

        if "environment" not in state.database_context:
            return AgentReply(
                text=(
                    "Which environment should I investigate — development, uat, or "
                    "production? For production targets I won't guess."
                ),
                status="clarification",
            )

        return await self._continue_investigation(state, investigation, channel, channel_account_id)

    async def _environment_for_instance(self, instance_hint: str) -> str | None:
        """Mirrors ServerRegistry.find_candidates' own matching (exact,
        substring, host, and normalized-for-spacing/punctuation/padding)
        so this convenience never asks "which environment" for a server
        the Gateway would actually have resolved unambiguously anyway —
        but only ever when it's unambiguous (more than one match here just
        means no auto-fill, never a guess; the Gateway still separately,
        independently re-resolves and validates whatever ends up in the
        actual tool call target regardless)."""
        hint = instance_hint.strip().lower()
        hint_normalized = normalize_server_reference(hint)
        matches: list[str] = []
        for s in await self._list_servers_cached():
            names = {s["id"].lower(), *(a.lower() for a in (s.get("aliases") or []))}
            host = (s.get("host") or "").lower()
            normalized_names = {normalize_server_reference(n) for n in names}
            if (
                hint in names
                or hint == host
                or hint_normalized in normalized_names
                or any(hint in n for n in names)
                or (host and hint in host)
                or any(hint_normalized in n for n in normalized_names)
            ):
                environment = s.get("environment")
                if environment:
                    matches.append(environment)
        unique = set(matches)
        return matches[0] if len(unique) == 1 else None

    async def _continue_investigation(
        self, state: ConversationState, investigation, channel: str, channel_account_id: str
    ) -> AgentReply:
        llm = self._llm_for(state)
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

            if investigation.consecutive_record_observations >= _MAX_CONSECUTIVE_RECORD_OBSERVATIONS:
                # Stop asking rather than wait out the rest of the turn
                # budget on a model that's already shown it isn't going to
                # conclude on its own — same safe fallback the turn cap
                # itself produces below, just reached sooner and without
                # spending further LLM calls on a pattern already proven
                # stuck.
                investigation.status = "CONCLUDED"
                return self._no_root_cause_reply(investigation)

            action = await llm.decide_next_action(
                problem_statement=self._problem_statement_for_llm(investigation),
                available_tool_ids=available_ids,
                transcript=investigation.transcript,
                turn_count=investigation.turn_count,
                tool_requirements=tool_requirements,
            )
            investigation.last_message = ""  # consumed — see the field's own docstring

            if isinstance(action, AskClarification):
                # Does NOT consume _MAX_INVESTIGATION_TURNS — a clarification
                # is the DBA narrowing down a target, not the model looping
                # on diagnostics, which is what that budget exists to bound.
                # Verified live: a multi-step targeting dialogue (environment
                # -> server -> database) burned most of the shared budget
                # before real diagnostics even started, then hit the cap
                # right as they began succeeding. Bounded independently
                # instead, so an unresolved back-and-forth still can't run
                # forever across many separate requests (turn_count alone
                # wouldn't catch that, since it's never incremented here).
                investigation.consecutive_record_observations = 0
                investigation.clarification_count += 1
                if investigation.clarification_count > _MAX_CLARIFICATION_TURNS:
                    investigation.status = "CONCLUDED"
                    return AgentReply(
                        text=(
                            "I still don't have enough information to proceed — "
                            "please restate what you'd like me to check, including "
                            "the environment, server, and database if relevant, in "
                            "one message."
                        ),
                        investigation_id=investigation.investigation_id,
                    )
                return AgentReply(
                    text=action.question, status="clarification", investigation_id=investigation.investigation_id
                )

            investigation.turn_count += 1
            investigation.clarification_count = 0

            if isinstance(action, RecordObservation):
                investigation.consecutive_record_observations += 1
                investigation.evidence.append(action.text)
                continue

            if isinstance(action, ProposeToolCall):
                investigation.consecutive_record_observations = 0
                reply = await self._submit_and_relay(
                    state, investigation, action, channel, channel_account_id
                )
                if reply is not None:
                    return reply
                continue  # executed successfully — loop for the next step

            if isinstance(action, Conclude):
                investigation.consecutive_record_observations = 0
                reply = self._finalize_conclude(investigation, action)
                if reply is not None:
                    return reply
                continue  # rejected as ungrounded — logged inside, try again

        # Turn budget exhausted without ever reaching action=conclude.
        # Verified live: a real investigation used its final 2 turns on
        # legitimate remediation attempts (kill_session, then cancel_query
        # as a fallback) that both turned up nothing to act on — genuinely
        # useful information — but hit the cap with zero turns left to
        # report it, and the DBA got the generic fallback below instead of
        # that. One last, bounded call gives the model a real chance to
        # explain the outcome first. No further tool calls are offered
        # (available_tool_ids=[]) — decide_next_action's own post-
        # validation turns any attempted one into an AskClarification,
        # which (like any non-Conclude response here) just falls through
        # to the same safe fallback as before; this can only ever add one
        # bounded call, never another loop.
        final_action = await llm.decide_next_action(
            problem_statement=self._problem_statement_for_llm(investigation)
            + "\n\nYou are out of further diagnostic or action turns. You MUST respond "
            "with action=conclude now, summarizing what was found and done so far — "
            "even if the root cause isn't fully confirmed, an honest \"here's what I "
            "found and tried\" is far more useful than nothing.",
            available_tool_ids=[],
            transcript=investigation.transcript,
            turn_count=investigation.turn_count,
            tool_requirements=None,
        )
        if isinstance(final_action, Conclude):
            reply = self._finalize_conclude(investigation, final_action)
            if reply is not None:
                return reply

        investigation.status = "CONCLUDED"
        return self._no_root_cause_reply(investigation)

    def _finalize_conclude(self, investigation, action: Conclude) -> AgentReply | None:
        """Builds the final reply for a Conclude action, or returns None
        if it's rejected as ungrounded — the caller decides what happens
        next (loop back for a retry mid-investigation, or fall through to
        the safe generic fallback if this was the one bounded last-chance
        call after the turn budget ran out)."""
        ungrounded = _ungrounded_identifiers(action, investigation)
        if ungrounded:
            # Don't accept an unverified claim at face value — the same
            # self-correction pattern used for a fixable DENIED response
            # above: feed back exactly what's wrong. Mid-investigation the
            # outer while loop's own check is what stops this if the model
            # keeps insisting; after the turn budget, the caller simply
            # doesn't retry and falls through to the safe fallback instead.
            note = (
                f"Your conclusion named {', '.join(ungrounded)}, which does not "
                "appear anywhere in this investigation's evidence or the "
                "DBA's own message — never state something as a finding "
                "unless it actually came from a tool result or what the "
                "DBA said. Revise your conclusion using only that."
            )
            investigation.transcript.append(
                {
                    "tool_id": "internal.grounding_check",
                    "reason": "Verifying the conclusion before reporting it.",
                    "result": {"rejected": ungrounded, "message": note},
                }
            )
            investigation.evidence.append(
                f"(a draft conclusion naming {', '.join(ungrounded)} was rejected — "
                "not found in any evidence gathered)"
            )
            logger.warning(
                "conclusion_rejected_ungrounded_identifiers",
                investigation_id=investigation.investigation_id,
                names=ungrounded,
            )
            return None
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

    @staticmethod
    def _no_root_cause_reply(investigation) -> AgentReply:
        """Shared by both places an investigation ends without ever
        reaching action=conclude: the turn cap itself, and the earlier,
        faster exit above once the model has shown it's stuck restating
        observations instead of concluding."""
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
        freeform investigation with no observations yet. Once a matched
        playbook's steps are all used up, append its conclusion guidance so
        the one LLM call that follows (interpreting everything the playbook
        gathered) knows what "done" looks like for this specific scenario,
        and is nudged to conclude now rather than keep investigating
        freeform on top of it. Separately, once the model has already
        recorded an observation without concluding, add an escalating nudge
        before `_MAX_CONSECUTIVE_RECORD_OBSERVATIONS` cuts it off entirely
        — verified live: a real model can restate the same finding as one
        observation after another instead of ever calling conclude, even
        when a plain, complete answer (including "nothing wrong was found"
        or "X isn't configured") was already available. And separately,
        when resuming after the DBA sent a new reply (rather than this
        being the investigation's first turn), that raw reply is included
        verbatim — see investigation.last_message's own docstring for why:
        decide_next_action needs to actually see what was just said to
        interpret a short answer to whatever it last asked, instead of that
        answer only ever being visible to (and often misclassified by) a
        fresh, context-free intent-extraction call."""
        playbook = get_playbook(investigation.playbook_id)
        problem = investigation.problem
        if investigation.last_message:
            problem += f"\n\nThe DBA just replied: {investigation.last_message!r}"
        if playbook is not None and investigation.playbook_step >= len(playbook.steps):
            problem = (
                f"{problem}\n\nYou just followed the '{playbook.name}' "
                f"playbook — see the transcript for what was checked and found. "
                f"{playbook.conclusion_guidance} If the evidence gathered is enough "
                "to conclude, conclude now rather than proposing further tool calls."
            )
        if investigation.consecutive_record_observations:
            problem += (
                "\n\nYou have already recorded an observation without concluding. Do "
                "not record another restatement of the same finding — if you have "
                "enough information to answer the DBA's question (\"nothing wrong "
                "was found\" or \"X isn't configured\" both count as complete "
                "answers), you MUST use action=conclude now instead."
            )
        return problem

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
            self_correctable_code = response.failure_code in _SELF_CORRECTABLE_DENIAL_CODES
            correctable = self_correctable_code and investigation.turn_count < _MAX_INVESTIGATION_TURNS
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
            if self_correctable_code:
                # Ran out of turns to self-correct — not a genuine policy/
                # permission fact. `response.message` here is deliberately
                # the raw, technical validation detail (see
                # tool_call_handler.py's INVALID_ARGUMENTS construction:
                # str(a Pydantic ValidationError)[:3]) — useful as feedback
                # for the LLM's own self-correction above, never meant for
                # a human. Verified live: a real DBA got that raw dump
                # (field names, "extra_forbidden", a pydantic.dev docs URL)
                # as the agent's entire reply, once the turn budget ran out
                # right on a self-correctable failure. Record the real
                # detail for audit, but show something a DBA can act on.
                logger.warning(
                    "self_correctable_denial_exhausted_turn_budget",
                    tool_id=action.tool_id,
                    failure_code=response.failure_code,
                    detail=response.message,
                )
                return AgentReply(
                    text=(
                        f"I ran out of attempts trying to get the request format "
                        f"right for {action.tool_id} — the last attempt failed "
                        f"validation ({response.failure_code}). Try rephrasing your "
                        "request, or ask again more specifically."
                    ),
                    status="denied",
                    investigation_id=investigation.investigation_id,
                )
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
        evidence_line = f"{action.tool_id}: {response.message}{_affected_summary(response.result)}"
        investigation.evidence.append(evidence_line)
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
            return self._status_reply(state)
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
            parts = stripped.split(maxsplit=1)
            return await self._handle_catalog_command(parts[1].strip() if len(parts) > 1 else None)
        if stripped == "/discover" or stripped.startswith("/discover "):
            parts = stripped.split(maxsplit=1)
            server_id = parts[1].strip() if len(parts) > 1 else None
            return await self._handle_discover_command(server_id, channel, channel_account_id)
        return None

    @staticmethod
    def _status_reply(state: ConversationState) -> AgentReply:
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

    async def _handle_meta_command(
        self, intent, state: ConversationState, channel: str, channel_account_id: str
    ) -> AgentReply:
        """Free-text equivalent of the exact slash commands above — never
        require the literal syntax (spec: the DBA should never be bound to
        a fixed message structure). `intent.instance_hint` doubles as the
        target server id for catalog/discover when one was named."""
        command = intent.meta_command
        if command == "help":
            return AgentReply(text=_HELP_TEXT)
        if command == "status":
            return self._status_reply(state)
        if command == "playbooks":
            return self._handle_playbooks_command()
        if command == "servers":
            return await self._handle_servers_command()
        if command == "catalog":
            return await self._handle_catalog_command(intent.instance_hint)
        if command == "discover":
            return await self._handle_discover_command(intent.instance_hint, channel, channel_account_id)
        if command in ("approve", "reject"):
            # Only ever acts on the one pending approval this conversation
            # already has (handle_approval_decision itself replies clearly
            # if there isn't one) — never a guess at *which* action, since
            # there is only ever the single one already shown to the DBA
            # via its approval card.
            return await self.handle_approval_decision(
                conversation_id=state.conversation_id,
                decision=command,
                channel=channel,
                channel_account_id=channel_account_id,
            )
        return AgentReply(text=_HELP_TEXT)  # unreachable given IntentExtraction's own enum

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

    async def _handle_catalog_command(self, server_id: str | None) -> AgentReply:
        if not server_id:
            return AgentReply(text="Which server's catalog would you like to see? (see /servers)")
        data = await self._tool_client.get_server_catalog(server_id)
        cat = (data or {}).get("catalog")
        if not cat:
            return AgentReply(text=f"No catalog for '{server_id}' yet — run /discover {server_id}")
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
        self, server_id: str | None, channel: str, channel_account_id: str
    ) -> AgentReply:
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
