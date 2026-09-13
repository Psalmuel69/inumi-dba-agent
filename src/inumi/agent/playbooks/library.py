"""A small library of fixed, named diagnostic sequences for the handful of
DBA scenarios that come up over and over (slow queries, high CPU, blocking,
...), plus a keyword matcher that picks one for a new investigation.

Why this exists (not a capability the Agent lacked — see the design note
below): freeform investigation already *can* call any read-only tool in any
order, and does so successfully. What it doesn't have on its own is a fixed,
predictable shape for a *known* scenario type — verified live, this is what
let one real investigation ("list the tables in AdventureWorks2019...") run
several unrelated diagnostics (blocking, wait stats, top queries) after
already having the answer, burn the whole turn budget, and return a vague
"no root cause found" instead of just answering. A playbook is a named,
reviewable, deterministic answer to "what do we check, in what order, for
this kind of problem" — and because each playbook's tool sequence is fixed
in advance, `_run_investigation_loop` can *execute* it without an LLM call
per step (only one call at the end, to interpret the gathered evidence and
conclude). That is a deliberate trade: a playbook trades "the model decides
every step" for "the model decides once, from a complete picture" — fewer
LLM round-trips per investigation (faster, cheaper, matters under the
_OVERALL_DEADLINE_SECONDS ceiling), and the same DBA question always
investigates the same way (auditable, reviewable ahead of time — every step
here still goes through the Gateway's own independent authorization/policy/
risk pipeline exactly like any other tool call; a playbook only decides
*which* read-only tool to propose next, never that it's allowed to run).

Every step here is a read-only diagnostic tool (`database.get_*`) with
either no arguments or arguments that are entirely fixed/self-contained
(e.g. `top_queries` ordered by the metric that scenario cares about) — a
playbook never proposes a write. A playbook only pre-selects *which*
diagnostics to run; if a Conclude action recommends a remediation, that
still goes through the normal LLM-proposes / Gateway-approves flow like any
other action (spec §7, §37) — nothing here bypasses approval.

When no playbook's triggers match the problem text, the investigation falls
back to the existing freeform loop exactly as before this feature existed —
this is additive, not a replacement."""

from __future__ import annotations

import dataclasses
import re


@dataclasses.dataclass(frozen=True)
class PlaybookStep:
    tool_id: str
    # Shown to the DBA as this step's `reason` — what this step is checking
    # and why, in the context of the scenario the playbook is for.
    purpose: str
    # Fixed, self-contained arguments (e.g. {"order_by": "cpu"}) — never
    # anything that needs to be inferred from the conversation (a session
    # id, a schema/table name); those stay in the freeform LLM-driven path.
    arguments: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Playbook:
    playbook_id: str
    name: str
    description: str
    # Trigger phrases matched case-insensitively, whole-word/whole-phrase
    # (never a bare substring — see _matches, this is what keeps "log" from
    # matching "login" and "lock" from matching "blocking" unintentionally
    # where that's not wanted, while still matching intended overlaps like
    # "lock" inside "deadlock" being a *separate*, deliberately-word-bounded
    # non-match).
    triggers: tuple[str, ...]
    steps: tuple[PlaybookStep, ...]
    # Steers the final conclude call once all steps have run — what "done"
    # looks like for this specific scenario.
    conclusion_guidance: str


def _step(tool_id: str, purpose: str, **arguments: object) -> PlaybookStep:
    return PlaybookStep(tool_id=tool_id, purpose=purpose, arguments=arguments)


# Ordered most-specific-first: a message can plausibly match more than one
# playbook's triggers (e.g. "deadlock" also concerns blocking), and the
# first match wins — see match_playbook.
PLAYBOOKS: tuple[Playbook, ...] = (
    Playbook(
        playbook_id="deadlocks",
        name="Deadlock Investigation",
        description="A reported deadlock or repeated deadlocking.",
        triggers=("deadlock", "deadlocks", "deadlocked", "deadlocking", "dead lock"),
        steps=(
            _step("database.get_deadlocks", "Pulling recent deadlock graphs."),
            _step("database.get_blocking_sessions", "Checking for blocking still in progress."),
            _step("database.get_running_queries", "Checking what's running now around the same objects."),
        ),
        conclusion_guidance=(
            "State which sessions/queries were involved in the deadlock graph(s), "
            "which was chosen as the deadlock victim, and whether the same access "
            "pattern is still occurring."
        ),
    ),
    Playbook(
        playbook_id="blocking",
        name="Blocking / Locking Investigation",
        description="Sessions blocked, stuck, or waiting on locks.",
        triggers=(
            "blocking", "blocked", "block chain", "locking", "lock wait",
            "stuck", "hanging", "won't complete", "not completing",
        ),
        steps=(
            _step("database.get_blocking_sessions", "Mapping the current blocking chain."),
            _step("database.get_running_queries", "Checking what the head blocker and waiters are running."),
            _step("database.get_wait_statistics", "Checking whether lock waits dominate overall wait time."),
            _step("database.get_sessions", "Checking how long the blocking session has been open."),
        ),
        conclusion_guidance=(
            "Identify the head blocker (session, query, how long it's held the "
            "lock) and who's waiting on it. Recommend cancelling the blocking "
            "query or killing the session only if it's clearly safe to do so — "
            "never propose it as the only option without saying what it would "
            "affect."
        ),
    ),
    Playbook(
        playbook_id="slow_queries",
        name="Slow Query Investigation",
        description="Queries or the database generally running slower than expected.",
        triggers=(
            "slow", "slowness", "slow query", "slow queries", "performance issue",
            "taking too long", "timing out", "timeout", "high latency", "latency",
            "queries are slow", "query is slow", "running slow",
        ),
        steps=(
            _step("database.get_health", "Establishing a baseline health snapshot."),
            _step("database.get_running_queries", "Checking what's executing right now."),
            _step("database.get_top_queries", "Ranking queries by duration.", order_by="duration"),
            _step("database.get_wait_statistics", "Checking what the engine is spending time waiting on."),
            _step("database.get_blocking_sessions", "Ruling out blocking as the cause."),
        ),
        conclusion_guidance=(
            "Name the specific query/queries responsible if the evidence points "
            "to one, and whether the cause looks like a missing index, stale "
            "statistics, blocking, or resource pressure (CPU/waits) rather than "
            "the query itself. Avoid introspection/system-catalog queries when "
            "picking 'the' slow query — THIS IS VERY IMPORTANT: a top-ranked row "
            "against the engine's own catalog/metadata views (Postgres: "
            "pg_catalog, information_schema; SQL Server: sys.*, "
            "INFORMATION_SCHEMA; MySQL/MariaDB: information_schema, "
            "performance_schema, mysql) is noise from monitoring/introspection, "
            "almost never the reported slowness, and must not be named as the "
            "root cause."
        ),
    ),
    Playbook(
        playbook_id="high_cpu",
        name="High CPU Investigation",
        description="Elevated or spiking CPU usage on the instance.",
        triggers=("cpu", "high cpu", "cpu spike", "cpu usage", "cpu utilization", "processor usage"),
        steps=(
            _step("database.get_health", "Establishing a CPU/resource baseline."),
            _step("database.get_top_queries", "Ranking queries by CPU consumption.", order_by="cpu"),
            _step("database.get_running_queries", "Checking what's currently executing."),
            _step("database.get_wait_statistics", "Checking for CPU-related (signal) waits."),
        ),
        conclusion_guidance=(
            "Name the query/queries driving CPU if the evidence points to one "
            "or a small number, and whether this looks like a single runaway "
            "query, a plan regression, or broad concurrent load. Avoid "
            "introspection/system-catalog queries when picking 'the' hot query "
            "— THIS IS VERY IMPORTANT: a top-ranked row against the engine's "
            "own catalog/metadata views (Postgres: pg_catalog, "
            "information_schema; SQL Server: sys.*, INFORMATION_SCHEMA; "
            "MySQL/MariaDB: information_schema, performance_schema, mysql) is "
            "noise from monitoring/introspection, almost never the reported "
            "CPU driver, and must not be named as the root cause."
        ),
    ),
    Playbook(
        playbook_id="high_memory",
        name="High Memory Investigation",
        description="Memory pressure, high memory usage, or out-of-memory conditions.",
        triggers=(
            "memory", "high memory", "memory pressure", "out of memory", "oom",
            "memory usage", "memory leak", "running out of memory",
        ),
        steps=(
            _step("database.get_health", "Establishing a memory baseline."),
            _step("database.get_configuration", "Checking configured memory limits."),
            _step(
                "database.get_top_queries",
                "Ranking queries by reads (buffer/memory pressure proxy).",
                order_by="reads",
            ),
            _step("database.get_running_queries", "Checking what's currently executing."),
        ),
        conclusion_guidance=(
            "State whether the configured memory limit looks undersized for the "
            "observed load, or whether a small number of queries are driving "
            "the pressure."
        ),
    ),
    Playbook(
        playbook_id="connections",
        name="Connection Saturation Investigation",
        description="Connection limits, refused/failed connections, or too many open sessions.",
        triggers=(
            "connection", "connections", "too many connections", "connection pool",
            "max connections", "can't connect", "cannot connect", "connection refused",
            "connection limit", "out of connections",
        ),
        steps=(
            _step("database.get_sessions", "Listing current sessions."),
            _step("database.get_health", "Establishing a baseline."),
            _step("database.get_configuration", "Checking the configured max-connections limit."),
        ),
        conclusion_guidance=(
            "State the current session count against the configured limit, and "
            "whether one application/account is holding a disproportionate "
            "number of connections. Lead with that comparison: if current usage "
            "is well under the configured max (comfortable headroom), say so "
            "plainly and stop there — do not manufacture a false alarm or "
            "invent tuning advice from otherwise-healthy numbers just because "
            "an investigation was run. Reserve genuine concern for when usage "
            "is meaningfully close to the limit, or one application/account "
            "holds a disproportionate share regardless of the overall total."
        ),
    ),
    Playbook(
        playbook_id="replication",
        name="Replication Investigation",
        description="Replication lag, Always On, or streaming replication issues.",
        triggers=(
            "replication", "replica", "replicas", "replication lag", "lagging",
            "always on", "availability group", "streaming replication", "standby",
        ),
        steps=(
            _step("database.get_replication_status", "Checking replication/Always On status and lag."),
            _step("database.get_health", "Establishing a primary-side health baseline."),
            _step("database.get_wait_statistics", "Checking for waits consistent with replication pressure."),
        ),
        conclusion_guidance=(
            "State the current lag (or sync state) per replica and whether it's "
            "within the expected range for this environment."
        ),
    ),
    Playbook(
        playbook_id="backups",
        name="Backup Health Investigation",
        description="Missed, failed, or overdue backups.",
        triggers=("backup", "backups", "backup failed", "backup job", "last backup", "backup status"),
        steps=(
            _step("database.get_backup_status", "Checking recent backup history and status."),
            _step("database.get_storage", "Checking available storage for the next backup."),
        ),
        conclusion_guidance=(
            "State the time and status of the most recent full/log backup and "
            "whether it's within the expected recovery-point objective for this "
            "environment."
        ),
    ),
    Playbook(
        playbook_id="storage",
        name="Storage / Transaction Log Investigation",
        description="Disk space, storage growth, or transaction log growth.",
        triggers=(
            "disk space", "disk full", "running out of space", "out of disk",
            "storage", "transaction log full", "log growing", "log full",
            "log is full", "wal growing", "out of space",
        ),
        steps=(
            _step("database.get_storage", "Checking overall storage/space utilization."),
            _step("database.get_transaction_log", "Checking transaction log / WAL usage."),
            _step("database.get_health", "Establishing a baseline."),
        ),
        conclusion_guidance=(
            "State current usage vs. capacity, and whether the transaction log "
            "specifically (vs. data files) is the growth driver — that usually "
            "points to a long-running or uncommitted transaction, or a paused "
            "log backup chain."
        ),
    ),
    Playbook(
        playbook_id="errors",
        name="Error Log Investigation",
        description="Errors, exceptions, or failures reported in the error log.",
        triggers=(
            "error log", "error logs", "errors in the log", "exceptions",
            "failing queries", "crashing", "keeps crashing", "seeing errors",
        ),
        steps=(
            _step("database.get_error_logs", "Pulling recent error log entries."),
            _step("database.get_health", "Establishing a baseline."),
            _step("database.get_running_queries", "Checking what's currently executing."),
        ),
        conclusion_guidance=(
            "Summarize the distinct error(s) found (not just a raw dump), how "
            "recent/frequent each is, and which looks most likely to be the "
            "reported problem."
        ),
    ),
    Playbook(
        playbook_id="general_health",
        name="General Health Check",
        description="A general request to check overall health/status, not a specific symptom.",
        triggers=(
            "health check", "how is", "how's", "overall status", "general status",
            "status check", "how is it doing", "how's it doing", "everything okay",
            "everything ok",
        ),
        steps=(
            _step("database.get_health", "Checking overall instance/database health."),
            _step("database.get_running_queries", "Checking current activity."),
            _step("database.get_wait_statistics", "Checking dominant wait types."),
            _step("database.get_storage", "Checking storage headroom."),
        ),
        conclusion_guidance=(
            "Give a concise overall status (healthy / attention needed) and "
            "call out anything that stood out, even if nothing looks urgent."
        ),
    ),
    Playbook(
        playbook_id="configuration_review",
        name="Configuration Tuning Review",
        description=(
            "A proactive review of server/database configuration for common, "
            "rule-of-thumb misconfigurations — not a request tied to a specific "
            "symptom. Scoped honestly: this flags settings that look clearly "
            "unreasonable by common rule-of-thumb ranges and obvious "
            "misconfigurations, not true capacity-based sizing (Inumi's server "
            "registry has no instance-class/hardware-sizing data to size "
            "against)."
        ),
        triggers=(
            "tune", "tuning", "configuration review", "review settings",
            "optimize configuration", "recommend settings", "config recommendations",
            "are our settings okay",
        ),
        steps=(
            _step("database.get_configuration", "Pulling current server/database configuration parameters."),
            _step(
                "database.get_health",
                "Establishing a baseline (connection count, size, uptime) to contextualize the settings.",
            ),
        ),
        conclusion_guidance=(
            "This is a rule-of-thumb review, not a capacity-sized recommendation "
            "— be explicit that no instance-class/hardware (CPU/RAM) data was "
            "available to size against, so never claim a setting is 'correctly "
            "sized for this hardware'; only flag values that look clearly "
            "unreasonable or left at an obvious default. Weigh the parameters "
            "relevant to whichever engine actually responded: Postgres — "
            "shared_buffers, effective_cache_size, work_mem, "
            "maintenance_work_mem, wal_buffers, checkpoint_completion_target, "
            "max_connections, default_statistics_target, random_page_cost, "
            "effective_io_concurrency, min_wal_size/max_wal_size, "
            "max_worker_processes, max_parallel_workers(_per_gather). SQL "
            "Server — 'max server memory (MB)'/'min server memory (MB)', 'max "
            "degree of parallelism', 'cost threshold for parallelism', and the "
            "max-connections-equivalent ('user connections'). MySQL/MariaDB — "
            "innodb_buffer_pool_size, innodb_log_file_size, max_connections, "
            "tmp_table_size/max_heap_table_size, innodb_flush_log_at_trx_commit, "
            "thread_cache_size. Cross-check against get_health's own numbers "
            "(e.g. active_connections vs. max_connections) where relevant, and "
            "say plainly when nothing looks obviously misconfigured rather than "
            "manufacturing tuning advice from reasonable-looking defaults."
        ),
    ),
)

_BY_ID: dict[str, Playbook] = {p.playbook_id: p for p in PLAYBOOKS}


def get_playbook(playbook_id: str | None) -> Playbook | None:
    if playbook_id is None:
        return None
    return _BY_ID.get(playbook_id)


def _matches(text: str, trigger: str) -> bool:
    return re.search(r"\b" + re.escape(trigger) + r"\b", text) is not None


def match_playbook(problem_text: str) -> Playbook | None:
    """Deterministic, zero-latency, zero-LLM-call keyword match — this
    selection has to be cheap and instant since it runs on every new
    investigation, before any LLM call. Returns None (freeform investigation,
    unchanged) when nothing matches."""
    text = (problem_text or "").lower()
    if not text.strip():
        return None
    for playbook in PLAYBOOKS:
        if any(_matches(text, trigger) for trigger in playbook.triggers):
            return playbook
    return None
