"""Deterministic, offline planner (spec §34).

Used when no LLM API key is configured, and by the entire automated test
suite — so CI never depends on a live LLM call, an API key, or model
output that can drift between runs. It is not a lesser security posture:
every decision it makes still passes the full Gateway pipeline, exactly
like a decision from a real model.

It reproduces the spec's worked investigation flow (§54/§68): check
health, then blocking, then propose terminating the head blocker — while
only ever inspecting *structural* fields of tool results
(`blocking_session_id`, row counts), never their free text, which is why
it is structurally immune to prompt injection.
"""

from __future__ import annotations

import re
from typing import Any

from inumi.agent.llm.base import LLMProvider
from inumi.agent.planner.actions import (
    AgentAction,
    Conclude,
    IntentExtraction,
    ProposeToolCall,
)

_DBA_KEYWORDS = re.compile(
    r"slow|performance|invest|block|deadlock|replicat|backup|storage|"
    r"transaction log|index|statistic|health|wait|query|session|failover|"
    r"restart|configuration|error log|cpu|latency|timeout",
    re.IGNORECASE,
)
_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|good (morning|afternoon|evening)|/help|/status)\b", re.IGNORECASE
)
_SESSION_ID_RE = re.compile(r"\bsession\s+(\d+)\b|\bkill\s+(\d+)\b", re.IGNORECASE)


class MockLLMProvider(LLMProvider):
    provider_name = "mock"

    def __init__(self) -> None:
        super().__init__("mock-planner")

    async def extract_intent(
        self,
        message: str,
        known_database_names: list[str],
        known_server_hints: list[str] | None = None,
    ) -> IntentExtraction:
        if _GREETING_RE.search(message):
            return IntentExtraction(is_dba_task=False, is_greeting_or_chitchat=True)

        database_hint = None
        lowered = message.lower()
        for name in known_database_names:
            if name.lower() in lowered:
                database_hint = name
                break
        if database_hint is None:
            camel_match = re.search(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b", message)
            if camel_match:
                database_hint = camel_match.group(0)

        # Longest match first so e.g. "postgres-local" wins over a shorter
        # substring like "local" also being a registered alias elsewhere.
        instance_hint = None
        for hint in sorted(known_server_hints or [], key=len, reverse=True):
            if hint and re.search(rf"\b{re.escape(hint.lower())}\b", lowered):
                instance_hint = hint
                break

        environment_hint = None
        if "prod" in lowered:
            environment_hint = "production"
        elif "uat" in lowered:
            environment_hint = "uat"
        elif "dev" in lowered:
            environment_hint = "development"

        is_dba_task = bool(_DBA_KEYWORDS.search(message)) or bool(_SESSION_ID_RE.search(message))
        return IntentExtraction(
            is_dba_task=is_dba_task,
            database_hint=database_hint,
            environment_hint=environment_hint,
            instance_hint=instance_hint,
            problem_summary=message.strip(),
        )

    async def decide_next_action(
        self,
        *,
        problem_statement: str,
        available_tool_ids: list[str],
        transcript: list[dict[str, Any]],
        turn_count: int,
        tool_requirements: dict[str, list[str]] | None = None,
    ) -> AgentAction:
        # tool_requirements isn't needed here — the deterministic scenarios
        # this planner proposes (kill_session, get_health, ...) never need
        # schema/table arguments, only real StructuredLLMProvider tool calls
        # do.
        executed = {t["tool_id"] for t in transcript}

        match = _SESSION_ID_RE.search(problem_statement)
        if match and "database.kill_session" in available_tool_ids and turn_count == 0:
            session_id = match.group(1) or match.group(2)
            return ProposeToolCall(
                tool_id="database.kill_session",
                arguments={"session_id": session_id, "reason": "Requested directly by DBA."},
                reason=f"You asked to terminate session {session_id}.",
            )

        if turn_count > 6:
            return Conclude(
                summary="Investigation reached its step limit without a confirmed root cause.",
                confidence="unable_to_confirm",
            )

        if "database.get_health" not in executed and "database.get_health" in available_tool_ids:
            return ProposeToolCall(
                tool_id="database.get_health", reason="Establish a baseline health snapshot."
            )

        if (
            "database.get_blocking_sessions" not in executed
            and "database.get_blocking_sessions" in available_tool_ids
        ):
            return ProposeToolCall(
                tool_id="database.get_blocking_sessions",
                reason="Reported slowness — checking for blocking chains first.",
            )

        blocking_call = next(
            (t for t in transcript if t["tool_id"] == "database.get_blocking_sessions"), None
        )
        blocking_rows = (blocking_call or {}).get("result", {}).get("rows", [])
        if blocking_rows:
            head_blocker = blocking_rows[0].get("blocking_session_id")
            if (
                head_blocker
                and "database.kill_session" not in executed
                and "database.kill_session" in available_tool_ids
            ):
                return ProposeToolCall(
                    tool_id="database.kill_session",
                    arguments={
                        "session_id": str(head_blocker),
                        "reason": f"Head blocker of a {len(blocking_rows)}-session blocking chain.",
                    },
                    reason=(
                        f"Session {head_blocker} is holding locks that block "
                        f"{len(blocking_rows)} other sessions."
                    ),
                )
            return Conclude(
                summary=f"A blocking chain originating from session {head_blocker} is affecting "
                f"{len(blocking_rows)} sessions.",
                likely_root_cause=f"Session {head_blocker} is holding a long-running lock.",
                confidence="likely",
                recommendation=f"Terminate session {head_blocker} to release the blocking chain.",
            )

        if (
            "database.get_running_queries" not in executed
            and "database.get_running_queries" in available_tool_ids
        ):
            return ProposeToolCall(
                tool_id="database.get_running_queries",
                reason="No blocking found — checking currently running queries.",
            )

        return Conclude(
            summary="No blocking chains or abnormal running queries were found.",
            confidence="unable_to_confirm",
            recommendation="Consider checking wait statistics and recent query plans.",
        )

    async def summarize_for_human(
        self, *, problem_statement: str, transcript: list[dict[str, Any]]
    ) -> str:
        steps = ", ".join(t["tool_id"] for t in transcript) or "no diagnostics"
        return f"Investigated: {problem_statement}. Steps run: {steps}."
