"""Conversation / investigation state (spec §25, §26, §37, §56).

Kept entirely in the Agent process (never a database credential or policy
decision in sight). Nothing stored here is ever treated as an authorization
grant — `database_context` is a convenience so "check it again" resolves to
the right target, but every tool call is still independently authorized by
the Gateway from scratch on every single request (spec §37).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

from inumi.common.ids import new_id


@dataclasses.dataclass
class PendingApproval:
    approval_id: str
    tool_id: str
    summary: str
    # The exact ToolCallRequest (as a plain dict) that produced this
    # approval requirement — resubmitted verbatim (with approval_id filled
    # in) once approved, so the Gateway's action-hash check always matches.
    request: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class InvestigationState:
    investigation_id: str
    problem: str
    target: dict[str, Any] = dataclasses.field(default_factory=dict)
    status: str = "INVESTIGATING"
    evidence: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    hypotheses: list[str] = dataclasses.field(default_factory=list)
    findings: list[str] = dataclasses.field(default_factory=list)
    recommendations: list[str] = dataclasses.field(default_factory=list)
    actions: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    transcript: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    turn_count: int = 0
    # Set once, when the investigation starts, from a deterministic keyword
    # match on the problem text (see agent.playbooks.library.match_playbook)
    # — None means this investigation is freeform, exactly as before
    # playbooks existed. Only the id is kept here (not the Playbook object
    # itself) so this state stays a plain, easily-inspectable/serializable
    # dataclass; look the object up via `get_playbook` when needed.
    playbook_id: str | None = None
    playbook_step: int = 0
    # How many record_observation actions the LLM has proposed *in a row*
    # (reset by any other action) — see orchestrator._MAX_CONSECUTIVE_
    # RECORD_OBSERVATIONS: a real model can get stuck restating the same
    # finding as one observation after another instead of ever calling
    # conclude, burning the whole turn budget on a case that was already
    # answerable after the first one.
    consecutive_record_observations: int = 0
    # Consecutive AskClarification turns — reset by any other action. Kept
    # separate from turn_count: a clarification is the DBA narrowing down a
    # target, not the model looping on diagnostics, so it doesn't spend the
    # shared diagnostic turn budget, but still needs its own bound (see
    # orchestrator._MAX_CLARIFICATION_TURNS) so an unresolved back-and-forth
    # can't run forever across many separate requests.
    clarification_count: int = 0
    # The DBA's raw reply when resuming an in-progress investigation — see
    # orchestrator.handle_message's resume path and _problem_statement_for_
    # llm. Set right before the next decide_next_action call, consumed
    # (cleared) by that same call so it doesn't linger and get repeated on
    # a later turn within the same request that has nothing to do with it.
    last_message: str = ""
    # Set the moment a write tool with a known, cheap, correlated read-only
    # re-check (see orchestrator._VERIFICATION_TOOLS_BY_WRITE_TOOL —
    # currently kill_session/cancel_query against get_blocking_sessions/
    # get_sessions/get_running_queries) executes, and cleared the moment one
    # of those re-check tools is itself proposed and executes — regardless
    # of what it finds; see `last_verification` for the actual verdict.
    # None means either no such write has happened yet this investigation,
    # or the one that did has already been followed by a re-check. Exists
    # because whether a remediation actually gets independently re-checked
    # was previously left entirely to the model's own discretion within its
    # turn budget — verified live, it sometimes does this unprompted, but
    # nothing forced it, so a DBA could get a clean "Completed" summary when
    # the underlying condition never actually cleared. See
    # `_finalize_conclude` (the check that acts on this) and
    # `_submit_and_relay` (where this is set/cleared).
    pending_verification: dict[str, Any] | None = None
    # The outcome of the most recently completed write-then-recheck
    # sequence this investigation has seen: "RESOLVED" (the recheck no
    # longer shows the condition the write targeted), "UNRESOLVED" (it
    # still does — the write executed but did not actually take effect), or
    # None (no such sequence has completed yet). Never set directly by the
    # model — derived structurally from a read tool's own result rows in
    # `_submit_and_relay`. Read by `_format_report` to state the real,
    # independently-checked outcome in the DBA-facing reply rather than
    # trusting the model's own free-text claim of success.
    last_verification: str | None = None


@dataclasses.dataclass
class ConversationState:
    conversation_id: str
    channel: str
    channel_thread_id: str
    channel_account_id: str
    database_context: dict[str, Any] = dataclasses.field(default_factory=dict)
    investigation: InvestigationState | None = None
    pending_approval: PendingApproval | None = None
    # Per-conversation LLM choice (set via the `/model` command). None -> use
    # the deployment's configured default. Never an authorization input.
    llm_provider: str | None = None
    llm_model: str | None = None
    updated_at: dt.datetime = dataclasses.field(
        default_factory=lambda: dt.datetime.now(dt.UTC)
    )


class ContextManager:
    """Process-local store. Swappable for a Redis-backed implementation
    behind the same interface for multi-instance deployments (spec §37's
    session continuity requirement doesn't require this to be durable across
    an Agent restart — a lost session simply starts a fresh investigation,
    which is the fail-closed choice over guessing stale state)."""

    def __init__(self) -> None:
        self._conversations: dict[str, ConversationState] = {}

    def get_or_create(
        self, conversation_id: str, channel: str, channel_thread_id: str, channel_account_id: str
    ) -> ConversationState:
        state = self._conversations.get(conversation_id)
        if state is None:
            state = ConversationState(
                conversation_id=conversation_id,
                channel=channel,
                channel_thread_id=channel_thread_id,
                channel_account_id=channel_account_id,
            )
            self._conversations[conversation_id] = state
        return state

    def start_investigation(self, state: ConversationState, problem: str) -> InvestigationState:
        state.investigation = InvestigationState(investigation_id=new_id("inv"), problem=problem)
        return state.investigation

    def touch(self, state: ConversationState) -> None:
        state.updated_at = dt.datetime.now(dt.UTC)
