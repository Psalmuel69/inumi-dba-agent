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
from inumi.common.models.tool import OperationType, ToolCallRequest, ToolCallResponse, ToolCallStatus
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

# Slack renders an @-mention as a literal `<@U0BOTID>` token in the message
# text delivered to the webhook (see channels/api/app.py's slack_webhook:
# `message=event.get("text", "")`, forwarded completely unmodified — nothing
# anywhere in the channels layer resolves or strips it). Verified live:
# sending "@Inumi DBA Agent /models" — the ordinary way of addressing the
# bot in a shared channel, often the only way to get its attention there —
# arrived here as "<@U0BOTID> /models", which matched neither
# `_handle_command_if_any`'s exact-match check nor any known free-form
# phrasing, and fell through into the normal DBA-task path: reported live as
# the exact, already-advertised `/models` command misrouting into "Which
# environment should I investigate?" instead of listing models. Stripped
# once, centrally, at the very top of handle_message (never by loosening
# each individual exact-match check) so every literal slash command, every
# free-form meta_command phrase, and every downstream problem_summary/
# last_message all see the DBA's actual words, never this delivery
# artifact. Deliberately only strips a LEADING or TRAILING mention token,
# never one in the middle of a message — a mid-message mention can
# meaningfully name a different person (e.g. "check with <@U999> about
# approving this"), and that must never be silently discarded.
_MENTION_TOKEN = r"<@[^>]+>"
_LEADING_MENTIONS_RE = re.compile(rf"^(?:\s*{_MENTION_TOKEN}\s*)+")
_TRAILING_MENTIONS_RE = re.compile(rf"(?:\s*{_MENTION_TOKEN}\s*)+$")


def _strip_bot_mention_noise(message: str) -> str:
    stripped = _LEADING_MENTIONS_RE.sub("", message)
    stripped = _TRAILING_MENTIONS_RE.sub("", stripped)
    return stripped.strip()


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


# Write tools with an obvious, cheap, correlated read-only re-check —
# scoped deliberately to session-termination-shaped writes first (the case
# this project has repeatedly hit live and gotten wrong: a real model has
# voluntarily said things like "Subsequent session and blocking checks
# confirmed that session X has been successfully terminated", but nothing
# ever forced that check, so a DBA could just as easily get a clean-
# sounding "Completed" when the session was never actually gone). A write
# like update_statistics/create_index/modify_configuration has no such
# cheap, obvious re-check (confirming it actually helped needs a follow-up
# performance observation, not one more tool call), so those deliberately
# stay out of this mapping for now — this is written as a lookup table,
# not a single hardcoded tool_id check, specifically so extending it to a
# future write tool is just one more entry, not a new mechanism.
_VERIFICATION_TOOLS_BY_WRITE_TOOL: dict[str, tuple[str, ...]] = {
    "database.kill_session": ("database.get_blocking_sessions", "database.get_sessions"),
    "database.cancel_query": (
        "database.get_blocking_sessions",
        "database.get_running_queries",
        "database.get_sessions",
    ),
}


def _verification_still_shows_condition(tool_id: str, session_id: str | None, result: dict | None) -> bool:
    """True if a post-write re-check's OWN result rows still show the
    exact session_id the write targeted — i.e. the write did not actually
    take effect. Deliberately narrow: only ever inspects the specific
    field(s) each of these read tools is known to populate (verified
    against `execution/adapters/*.py` — `session_id` for
    get_sessions/get_running_queries, `blocked_session_id`/
    `blocking_session_id` for get_blocking_sessions), and never guesses
    when session_id is unknown (e.g. a write whose arguments were stripped
    before this ever ran) or the result is missing/oddly shaped — those
    cases fall through to "not shown as still present", which is the safe
    direction: this function's only job is to catch a confirmed-not-
    resolved case, never to manufacture one from ambiguous data."""
    if not session_id:
        return False
    rows = (result or {}).get("rows") or []
    if not rows:
        return False
    keys = (
        ("blocked_session_id", "blocking_session_id")
        if tool_id == "database.get_blocking_sessions"
        else ("session_id",)
    )
    return any(str(row.get(key)) == str(session_id) for row in rows for key in keys)


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
    "/discover, /playbooks, /approvers\n\nYou never need the exact slash "
    "syntax for any of these — plain language works too (e.g. \"what "
    "servers do you have\" instead of /servers)."
)

# A general, honest answer to "who can approve requests from you?" and
# similar RBAC/approval-policy questions — deliberately NOT a dump of
# config/policy.yaml (that file is loaded only by the Gateway process, see
# POLICY_MODEL.md, and its exact per-role/per-environment grants are never
# meant to live in an agent prompt or reply) and deliberately not a per-role
# lookup this layer has no way to compute correctly anyway (the Agent's own
# ToolClient only ever sees a coarse allowed_roles list per tool, filtered
# to the asking DBA's own role — see tool_client.available_tools — never
# the full ALLOW/DENY/REQUIRES_APPROVAL table for every role). What follows
# is the general shape of the model as documented in POLICY_MODEL.md and
# implemented in gateway/domain/approval.py — true for every deployment,
# never a specific grant — plus a pointer to where a DBA gets the exact
# answer for their own request.
_APPROVAL_MODEL_TEXT = (
    "Approval requirements are decided by the DBA Control Gateway's policy "
    "engine, not by me — they depend on your own DBA role, the specific "
    "action, and the target environment, so there's no single fixed answer "
    "I can give in the abstract. In general: routine read-only checks never "
    "need approval; higher-risk write actions typically require a more "
    "senior DBA role or a separate, independent approver (you can never "
    "approve your own request); and the most critical actions (an instance "
    "restart or a failover) require two different qualified approvers, not "
    "just one. Whenever one of your own requests actually needs approval, "
    "I'll show you exactly what's required at that moment — for your "
    "role's specific permissions ahead of time, check with your "
    "organization's RBAC documentation or a DBA_MANAGER."
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
        message = _strip_bot_mention_noise(message)
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
            switched_instance = state.database_context.get("instance") != intent.instance_hint
            if not intent.database_hint and switched_instance:
                # A remembered database (see _submit_and_relay) belongs to
                # whatever instance was previously in play — moving to a
                # different one makes it stale, and it's never safe to
                # assume the new server even has a database by that name.
                state.database_context.pop("database", None)
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
                elif switched_instance:
                    # Mirrors the database-goes-stale-on-switch logic just
                    # above, for the same reason: a remembered environment
                    # belongs to whatever instance was previously in play
                    # too. Moving to a different, unregistered/ambiguous
                    # server whose environment can't be auto-resolved must
                    # not silently keep asserting the OLD instance's
                    # environment for this new, unnamed-environment one —
                    # that's exactly the guess the spec forbids ("for
                    # production targets I won't guess"). Drop it so the
                    # check below asks instead of carrying over a value
                    # that may now simply be wrong.
                    state.database_context.pop("environment", None)

        # A fresh investigation only ever needs to ask for the environment
        # when it is genuinely unknown anywhere in this conversation — never
        # just because *this* message alone didn't repeat it. Verified live:
        # once the DBA had already established development/postgres-local a
        # few messages earlier, a later plain follow-up ("so what database
        # is the copy activity happening on?") with no environment/instance
        # wording of its own still asked "which environment should I
        # investigate?" again. state.database_context is the real source of
        # truth for "environment" — carried across investigations in this
        # same conversation exactly like instance/database already are
        # above — so this checks it directly rather than re-deriving the
        # answer from intent.environment_hint alone, which only ever
        # reflects this one message.
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
        #
        # Deliberately includes a tool whose required list is EMPTY (e.g.
        # every NoArgs read tool), rather than omitting it — this used to
        # filter those out (`if (reqs := ...)`, false for an empty list),
        # which was itself a live, repeated finding: get_blocking_sessions
        # and get_sessions (both NoArgs) kept getting called with an extra
        # `reason` or `session_id`/`database_name` folded into `arguments`,
        # because the model was never actually told those specific tools
        # need nothing there — it only ever saw entries for tools that DO
        # need something, and reasonably (if wrongly) generalized from the
        # `arguments` schema's superset of possible keys (`session_id`,
        # `reason`, ... — real requirements for OTHER tools). An explicit
        # `[]` entry is a positive "this tool needs nothing", not silence
        # the model has to interpret on its own.
        tool_requirements = {t.tool_id: t.argument_schema.get("required", []) for t in available}
        # Defense-in-depth backstop for the same finding: every available
        # tool's complete set of allowed `arguments` keys (not just the
        # required ones — e.g. get_top_queries' optional order_by/limit
        # still need to survive this), used by `_submit_and_relay` to
        # silently drop any key the model adds anyway despite the prompt
        # guidance above, before a request ever reaches the Gateway. Belt-
        # and-suspenders: the prompt fix alone was verified live to still
        # occasionally slip (a smaller/weaker model, or a fresh provider
        # this prompt hasn't been tuned against), and stripping here is
        # always safe — a key the tool's own schema wouldn't accept can
        # never have been meant for it.
        tool_allowed_arguments = {
            t.tool_id: set(t.argument_schema.get("properties", {})) for t in available
        }
        # Each tool's operation_type, straight from the same `/v1/tools`
        # response already fetched above — nothing new to thread from the
        # Gateway, this info was already here. Used by `_submit_and_relay`
        # as a defense-in-depth confirmation (alongside
        # `_VERIFICATION_TOOLS_BY_WRITE_TOOL`'s own tool_id membership) that
        # a tool this is about to treat as "a write needing a post-hoc
        # check" is actually still classified as OperationType.WRITE by the
        # tool catalog right now, not stale knowledge baked into this file.
        tool_operation_types = {t.tool_id: t.operation_type for t in available}

        return await self._run_investigation_loop(
            state,
            investigation,
            available_ids,
            channel,
            channel_account_id,
            llm,
            tool_requirements,
            tool_allowed_arguments,
            tool_operation_types,
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
        tool_allowed_arguments: dict[str, set[str]] | None = None,
        tool_operation_types: dict[str, OperationType] | None = None,
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
                    state,
                    investigation,
                    step_action,
                    channel,
                    channel_account_id,
                    tool_allowed_arguments,
                    tool_operation_types,
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
                    state,
                    investigation,
                    action,
                    channel,
                    channel_account_id,
                    tool_allowed_arguments,
                    tool_operation_types,
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
            # final_chance=True: no tool calls were even offered for this
            # last attempt (available_tool_ids=[] above), so a still-
            # pending post-write verification can no longer be fixed by
            # asking again — accept the conclusion but let `_format_report`
            # state plainly that it was never independently re-checked,
            # rather than rejecting into the generic no-root-cause
            # fallback and losing everything the investigation actually did.
            reply = self._finalize_conclude(investigation, final_action, final_chance=True)
            if reply is not None:
                return reply

        investigation.status = "CONCLUDED"
        return self._no_root_cause_reply(investigation)

    def _finalize_conclude(
        self, investigation, action: Conclude, *, final_chance: bool = False
    ) -> AgentReply | None:
        """Builds the final reply for a Conclude action, or returns None
        if it's rejected — the caller decides what happens next (loop back
        for a retry mid-investigation, or fall through to the safe generic
        fallback if this was the one bounded last-chance call after the
        turn budget ran out). Two independent things can reject a
        conclusion: it names something ungrounded (see
        `_ungrounded_identifiers`), or it's trying to conclude with a
        write's real-world effect still unverified (see
        `investigation.pending_verification`'s own docstring for why this
        exists) — the latter only applies mid-investigation
        (`final_chance=False`): the one bounded last-chance call offers no
        tool calls at all (`available_tool_ids=[]`), so there is no way
        left for the model to actually go check, and rejecting it there
        would only throw away everything the investigation found in favor
        of the generic no-root-cause fallback. `_format_report` is what
        states the true, structurally-derived verification outcome in
        that case instead of trusting the model's own wording."""
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

        if not final_chance and investigation.pending_verification is not None:
            # Structural enforcement of the project's own "SYSTEM VERIFIES
            # -> INUMI REPORTS THE ACTUAL OUTCOME" principle (README.md's
            # lifecycle diagram): a write that just executed must not be
            # treated as license to conclude "Completed" without an
            # independent re-check — never mark an incident resolved
            # merely because an action was submitted. Same self-correction
            # shape as the grounding check just above: reject, feed back
            # exactly what's missing, and let the loop's own turn budget
            # bound how many times this can happen — never an unbounded
            # wait for the model to eventually decide to check.
            pending = investigation.pending_verification
            verification_tools = " or ".join(pending["verification_tools"])
            note = (
                f"{pending['tool_id']} executed, but nothing has independently "
                f"re-checked yet whether it actually took effect — never mark an "
                f"action as resolved merely because it was submitted. Call "
                f"{verification_tools} to confirm the real-world outcome before "
                "concluding."
            )
            investigation.transcript.append(
                {
                    "tool_id": "internal.verification_check",
                    "reason": "Verifying the remediation's real-world effect before reporting it.",
                    "result": {"pending_tool_id": pending["tool_id"], "message": note},
                }
            )
            investigation.evidence.append(
                f"(a draft conclusion after {pending['tool_id']} was rejected — "
                "not yet independently verified)"
            )
            logger.warning(
                "conclusion_rejected_pending_verification",
                investigation_id=investigation.investigation_id,
                tool_id=pending["tool_id"],
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
        and is nudged to conclude now if the evidence already supports it.
        That nudge deliberately states both directions, not just the
        "conclude now" one: `_next_playbook_action` returning None here
        does NOT mean the model is limited to conclude-only from this point
        — `available_tool_ids` handed to this same decide_next_action call
        is still the full, unrestricted tool menu (see
        `_continue_investigation`/`_run_investigation_loop`), so an
        inconclusive playbook is explicitly told it may propose one or more
        further freeform diagnostic tool calls, exactly like the original
        fully-freeform path, before ever concluding — a playbook only ever
        pre-decides a *known* scenario's fixed opening sequence, never a
        ceiling on what can be investigated afterward. Any such extra call
        still spends from the same shared `_MAX_INVESTIGATION_TURNS` budget
        as everything else (see the loop itself), so this can never let an
        investigation run longer than a fully freeform one could. Separately,
        once the model has already
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
                "to conclude, conclude now rather than proposing further tool calls. "
                "If it is NOT enough, propose one or more additional read-only "
                "diagnostic tool calls that would specifically fill the gap, before "
                "concluding — you are not limited to this playbook's fixed steps; "
                "any tool in the available list is yours to use."
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

    @staticmethod
    def _strip_unschematized_arguments(
        action: ProposeToolCall, tool_allowed_arguments: dict[str, set[str]] | None
    ) -> dict:
        """Defense-in-depth backstop for a live, repeated finding: even with
        the prompt telling the model each tool's real schema (see
        `_continue_investigation`'s `tool_requirements`/`tool_allowed_arguments`
        and `StructuredLLMProvider._ACTION_SYSTEM`), a real model sometimes
        still folds an extra key into `arguments` that tool's schema
        forbids — most often `reason` (confused with `ProposeToolCall`'s own
        top-level `reason`) or a `session_id`/`database_name` it decided was
        relevant. The Gateway's own argument model is `extra="forbid"`, so
        an unstripped extra key is rejected outright as INVALID_ARGUMENTS —
        self-correctable (see `_SELF_CORRECTABLE_DENIAL_CODES`), but always
        at the cost of one wasted turn and Gateway round-trip.
        Silently dropping a key the tool's own schema never declared is
        always safe: it can never have been a value that tool would have
        accepted anyway. `tool_allowed_arguments` being None (the tool
        wasn't in the available list, or no schema info was supplied — see
        the unit tests in test_orchestrator_self_correction.py that call
        this without it) skips stripping entirely rather than guessing."""
        allowed = (tool_allowed_arguments or {}).get(action.tool_id)
        if allowed is None:
            return action.arguments
        extra = set(action.arguments) - allowed
        if not extra:
            return action.arguments
        logger.info(
            "stripped_unschematized_tool_arguments",
            tool_id=action.tool_id,
            dropped=sorted(extra),
        )
        return {k: v for k, v in action.arguments.items() if k in allowed}

    async def _submit_and_relay(
        self,
        state: ConversationState,
        investigation,
        action: ProposeToolCall,
        channel: str,
        channel_account_id: str,
        tool_allowed_arguments: dict[str, set[str]] | None = None,
        tool_operation_types: dict[str, OperationType] | None = None,
    ) -> AgentReply | None:
        arguments = self._strip_unschematized_arguments(action, tool_allowed_arguments)
        request = ToolCallRequest(
            tool_id=action.tool_id,
            arguments=arguments,
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

        # Remember a database once it's actually been resolved — by the
        # DBA naming it, or (verified live, the recurring complaint this
        # exists for) by the model self-correcting an INVALID_TARGET
        # rejection within this same investigation — so a later message in
        # this conversation never has to re-supply it. Mirrors how
        # environment/instance already persist in state.database_context.
        # Safe to persist on any status except a DENIED specifically
        # *about* the target (INVALID_TARGET): the Gateway's own target
        # resolution runs before authorization/policy/risk/rate-limiting
        # in its pipeline, so EXECUTED, APPROVAL_REQUIRED, FAILED (an
        # adapter-level problem, unrelated to target correctness), or a
        # DENIED for any other reason (RBAC, rate limit, ...) all still
        # mean this exact database name was independently accepted as
        # valid for this server — never a guess of our own.
        database = request.target.get("database")
        if database and not (
            response.status == ToolCallStatus.DENIED and response.failure_code == "INVALID_TARGET"
        ):
            state.database_context["database"] = database

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
        self._update_pending_verification(investigation, action, arguments, response, tool_operation_types)
        return None

    @staticmethod
    def _update_pending_verification(
        investigation,
        action: ProposeToolCall,
        arguments: dict,
        response: ToolCallResponse,
        tool_operation_types: dict[str, OperationType] | None,
    ) -> None:
        """Sets or clears `investigation.pending_verification`/
        `last_verification` (see their own docstrings on
        `InvestigationState`) — the structural half of the post-remediation
        verification mechanism; `_finalize_conclude`/`_format_report` are
        what act on what this records. Called for every EXECUTED tool call,
        not just writes — most calls match neither branch below and this
        is a no-op for them.

        `tool_operation_types.get(action.tool_id) == OperationType.WRITE`
        is checked as a defense-in-depth confirmation (mirroring
        `_strip_unschematized_arguments`'s own belt-and-suspenders
        reasoning) that the tool catalog *currently* still classifies this
        tool_id as a write, not stale assumption baked into this file. When
        `tool_operation_types` is None (older/direct test call sites, or a
        code path that never fetched the catalog), this falls back to
        trusting `_VERIFICATION_TOOLS_BY_WRITE_TOOL`'s own tool_id
        membership alone rather than skipping the check outright — that
        mapping only ever lists tools declared WRITE in
        `gateway/domain/tool_catalog.py` to begin with."""
        if investigation.pending_verification is not None and action.tool_id in (
            investigation.pending_verification["verification_tools"]
        ):
            still_present = _verification_still_shows_condition(
                action.tool_id, investigation.pending_verification.get("session_id"), response.result
            )
            investigation.last_verification = "UNRESOLVED" if still_present else "RESOLVED"
            investigation.pending_verification = None
            return

        verification_tools = _VERIFICATION_TOOLS_BY_WRITE_TOOL.get(action.tool_id)
        if verification_tools is None:
            return
        is_write = tool_operation_types is None or (
            tool_operation_types.get(action.tool_id) == OperationType.WRITE
        )
        if not is_write:
            return
        investigation.pending_verification = {
            "tool_id": action.tool_id,
            "verification_tools": verification_tools,
            "session_id": arguments.get("session_id"),
            "reason": action.reason,
        }
        investigation.last_verification = None

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
        if stripped == "/approvers":
            return self._handle_approvers_command()
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
        if command == "models":
            return await self._handle_model_command(state, "/models")
        if command == "approvers":
            return self._handle_approvers_command()
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
            if cat:
                # `databases` is already the full list of discovered database
                # NAMES on this same /v1/catalog/servers response (see
                # gateway/api/routers/catalog.py's list_servers — the same
                # field `_known_database_names` already reads elsewhere) —
                # free to include here too, no extra discovery call or new
                # tool needed. Answers "what databases do you have access
                # to?" directly instead of only a bare count, which was the
                # one real content gap in this reply. Truncated defensively;
                # the full per-database detail still lives behind
                # /catalog <id>.
                names = cat.get("databases") or []
                preview = ", ".join(names[:8]) + (", …" if len(names) > 8 else "")
                db_part = f" ({preview})" if preview else ""
                summary = (
                    f"{cat['database_count']} databases{db_part}, discovered "
                    f"{(cat['discovered_at'] or '')[:16]}"
                )
            else:
                summary = "not yet discovered — run /discover"
            lines.append(
                f"- {s['id']}  [{s['environment']}/{s['platform']}, {s['criticality']}]  {summary}"
            )
        return AgentReply(text="Registered servers:\n" + "\n".join(lines))

    @staticmethod
    def _handle_approvers_command() -> AgentReply:
        return AgentReply(text=_APPROVAL_MODEL_TEXT)

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
        verification_note = AgentOrchestrator._verification_note(investigation)
        if verification_note:
            lines.append(verification_note)
        return "\n".join(lines)

    @staticmethod
    def _verification_note(investigation) -> str | None:
        """States the real, independently-checked outcome of a write this
        investigation performed — structurally derived from
        `investigation.last_verification`/`pending_verification` (see
        their own docstrings), never from the model's own free-text
        Conclude, so a DBA is never left trusting a bare "Completed" for a
        write whose real-world effect was never actually checked. Three
        distinct, deliberately-worded outcomes (never collapsed into one
        generic "done"): independently confirmed resolved, independently
        confirmed NOT resolved, or executed but never independently
        checked at all (reachable only via the last-chance Conclude call —
        see `_finalize_conclude`'s `final_chance`)."""
        if investigation.last_verification == "RESOLVED":
            return "Verification: independently re-checked afterward and confirmed resolved."
        if investigation.last_verification == "UNRESOLVED":
            return (
                "Verification: independently re-checked afterward — this did NOT "
                "actually resolve the condition; further action is likely still needed."
            )
        if investigation.pending_verification is not None:
            pending = investigation.pending_verification
            return (
                f"Verification: {pending['tool_id']} executed, but I ran out of turns "
                "before independently re-checking whether it actually took effect — "
                "treat this as executed but NOT independently verified."
            )
        return None
