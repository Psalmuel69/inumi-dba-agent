# Architecture

## Topology

```
DBA
 │  Slack / Teams
 ▼
Channel Adapter (channels/)
 │  verify signature/token, resolve identity (UX check), forward
 ▼
AI DBA Agent (agent/)
 │  LLM-backed planning; no DB credential; every action is a "proposal"
 ▼  Tool Request (untrusted)
DBA CONTROL GATEWAY (gateway/)
 │  Registry → Args → Target → AuthZ → RateLimit → Policy → Risk → Approval
 ▼  Authorized, scoped execution instruction
Execution Service (execution/)
 │  the ONLY component with database credentials/network access
 ├─→ SQLServerAdapter
 ├─→ PostgreSQLAdapter
 └─→ MySQLAdapter (MySQL + MariaDB)
        │
        ▼
     DATABASE
```

Every arrow above is a real network hop (HTTP + a signed, audience-scoped
service token — `inumi.common.service_auth`), not a function call inside one
process. That's deliberate: the trust boundary is the *service* boundary,
not a class boundary a bug could accidentally erase.

## Why four services and not one

| If merged into... | What breaks |
|---|---|
| Agent + Gateway | The LLM process could reach the authorization/policy/approval code paths directly — no network hop to intercept, audit, or rate-limit independently of the model. |
| Gateway + Execution | The Gateway (which parses arbitrary-ish inputs, evaluates policy, and is closer to attacker-influenced data) would hold database credentials. |
| Everything | No independent audit trail, no way to scale/patch/credential each concern separately, no way to run the Agent in a lower-trust network zone than the Execution Service. |

## Request lifecycle (a tool call)

1. **Channel Adapter** (`channels/`) verifies the inbound webhook
   (`channels/slack/signature.py`, `channels/teams/auth.py`), independently
   resolves the sender's enterprise identity via `IdentityProvider`
   (UX-level convenience check — see below), and forwards
   `{channel, channel_account_id, message}` to the Agent over HTTP with a
   signed service token.
2. **Agent** (`agent/orchestrator.py`) resolves an `LLMProvider` for the
   conversation via `agent/llm/registry.py` (the DBA's `/model` choice, or
   the configured default — Anthropic / OpenAI / Gemini / DeepSeek, or the
   deterministic offline planner when no key is set) and, in a bounded
   loop, asks it to classify intent and decide the next investigative step
   — always as a *typed* `AgentAction` (`agent/planner/actions.py`), never
   free text. A `ProposeToolCall` becomes a `ToolCallRequest` sent to the
   Gateway via `agent/tool_client.py`. The choice of model has no bearing
   on the security boundary: every proposal, from any provider, goes
   through the identical Gateway pipeline.
3. **Gateway** (`gateway/domain/tool_call_handler.py`) runs the full
   pipeline: tool registry lookup → argument schema validation → target
   parsing/enrichment → server resolution + catalog validation → **independent identity
   re-resolution** (see below) → rate limiting → policy evaluation → risk
   assessment → approval creation-or-verification → dispatch to the
   Execution Service → data minimization → audit.
4. **Execution Service** (`execution/service.py`) is hit only by the
   Gateway, with a service token scoped to its own audience. It resolves a
   credential via `CredentialProvider`, opens a connection, and dispatches
   to one typed `DatabaseAdapter` method. It returns raw (unmasked) rows —
   masking is the Gateway's job, so there's exactly one place that decision
   is made.
5. Gateway applies `DataMinimizer`, records an `AuditEventRecord`, and
   returns a structured `ToolCallResponse` (EXECUTED / APPROVAL_REQUIRED /
   DENIED / FAILED) — never a stack trace, never raw DB error text.
6. Agent relays the result (or an approval card) back through the Channel
   Adapter to the human.

## Investigation loop: freeform vs. playbook-driven

`orchestrator.py::_run_investigation_loop` bounds every investigation to
`_MAX_INVESTIGATION_TURNS` (6) steps so a confused model can't loop forever.
It also bounds a narrower failure mode independently of the turn cap:
verified live, a model can get stuck restating the same finding as one
`record_observation` after another instead of ever calling `conclude`, even
once a plain, complete answer ("no replica configured") was already clear.
`_MAX_CONSECUTIVE_RECORD_OBSERVATIONS` (2, reset by any other action) stops
asking once that pattern is clearly stuck, using whatever evidence already
exists rather than waiting out the rest of the turn budget on further calls
that were never going to conclude either.

Within the turn/observation bounds, a step is decided one of two ways:

- **Freeform** (the default, and the only mode before playbooks existed):
  every turn calls `LLMProvider.decide_next_action` — the model picks the
  next tool from the ones it was offered, or concludes.
- **Playbook-driven**: on a new investigation, `agent.playbooks.library
  .match_playbook` runs a deterministic, zero-LLM-call keyword match
  against the problem text. For 11 known scenarios (slow queries, high CPU,
  high memory, blocking, deadlocks, connection saturation, replication lag,
  backup health, storage/transaction log, error logs, general health), this
  picks a fixed, named sequence of read-only diagnostic calls. Each step of
  a matched playbook is submitted directly — **no LLM call in between** —
  and the LLM is asked only once, after the sequence completes, to
  interpret everything gathered and conclude (nudged by the playbook's own
  conclusion guidance). Unmatched problem text runs fully freeform,
  unchanged.

Why this exists: freeform investigation was already *capable* of running
any read-only tool in any order and reaching a correct answer — a playbook
adds no new capability. What it fixes is that, for a *known* scenario type,
the freeform loop had no fixed shape or stopping point: verified live, one
real investigation ran several unrelated diagnostics after it already had
its answer and exhausted the turn budget without concluding. A playbook is
a named, reviewable, deterministic answer to "what do we check, in what
order, for this kind of problem" — and because the sequence is fixed in
advance, it also means fewer LLM round-trips per investigation (each one
a chance for a malformed completion or added latency), which matters under
the per-decision latency ceiling below. A playbook only ever pre-selects
*which* read-only diagnostics to run — a recommended remediation, or any
write, still goes through the normal LLM-proposes / Gateway-approves flow
exactly like a freeform investigation's.

**Grounding the conclusion.** The Pydantic/discriminated-union validation
that gates every `AgentAction` (`agent.planner.actions.agent_action_adapter`)
only ever checks an action's *shape* — a `Conclude`'s `summary`/
`likely_root_cause`/`recommendation` are free text with nothing stopping the
model from stating something that never happened. Verified live: a real
conclusion named three CamelCase-looking table names that don't exist in
the database at all, instead of the real ones its own tool call had
actually returned. `orchestrator._ungrounded_identifiers` checks any such
name against everything the investigation actually gathered (transcript,
evidence, and the DBA's own problem statement — so the DBA's own
terminology is never mistaken for a hallucination) and, on a miss, rejects
the conclusion and gives the model one more bounded try — the same
self-correction pattern used for a fixable Gateway denial above, capped by
the same turn count as everything else.

## Latency ceiling on a single LLM decision

Each layer of `StructuredLLMProvider`'s resilience (per-call timeout →
model-fallback with cooldown, Gemini-specific → one same-model retry) is
individually reasonable but has no bearing on the others' worst case —
verified live, their product left one real decision hanging for minutes
with a provider under sustained load. `_OVERALL_DEADLINE_SECONDS` (20s)
wraps the whole thing: no matter how many retries or model switches happen
underneath, a single `decide_next_action`/`extract_intent` call degrades to
a clear "try again" message within ~20s, never longer. This is the actual
production guarantee — not any individual timeout's own value.

## Instance-wide diagnostics don't demand a database

Every read tool used to default to `required_target_scope = ["environment",
"instance", "database"]` (`gateway/domain/tool_catalog.py`), so a DBA saying
"what's running on postgres-local" with no database named got rejected —
`INVALID_TARGET`: "Which database on postgres-local?" — even though the
underlying diagnostic never needed one. Checked against all three adapters
(`execution/adapters/{postgresql,sqlserver,mysql}.py`): SQL Server's DMVs and
MySQL's `information_schema`/`performance_schema` views were already
genuinely instance-wide for sessions, blocking, deadlocks, running queries,
wait stats, replication, backups, configuration, and error logs — no
per-database filter in the SQL. PostgreSQL's adapter was the one with a real
bug: it added an artificial `where datname = current_database()` to most of
these, even though `pg_stat_activity`/`pg_locks`/`pg_stat_database` are
natively cluster-wide in Postgres. That filter is now removed, and a
`datname`/`database_name` column is surfaced on the affected rows so the
result itself says which database(s) are involved.

`database.get_health`, `get_version`, `get_sessions`, `get_blocking_sessions`,
`get_deadlocks`, `get_running_queries`, `get_wait_statistics`,
`get_replication_status`, `get_backup_status`, `get_configuration`, and
`get_error_logs` now declare `required_target_scope = ["environment",
"instance"]` — no database required. Tools that are genuinely
database-scoped on at least one engine (`get_storage`,
`get_transaction_log`, `get_tables`, `get_indexes`/`get_statistics`,
`get_query_plan`/`get_top_queries`) are unchanged. `execution/service.py`
already fell back to the credential's own default database when none is
given (`if request.database: ...`) — no change needed there.

This is what lets the agent *investigate* which database is affected (e.g.
"check what's running/blocking on X") instead of only ever being told, or
only remembering one once a DBA happens to name it — a DBA asking about an
incident often doesn't know the database yet; that's the point of asking.

## The identity re-resolution point (why "UX check" ≠ "security boundary")

Channel Adapters and the Agent both *can* check whether a channel account is
a recognized DBA — this is a UX nicety (fail fast with a friendly message
instead of round-tripping to the LLM for someone who was never going to be
authorized). **But `ToolCallRequest` carries only `channel` +
`channel_account_id` — never a `VerifiedIdentity` object, never a role.**
The Gateway (`gateway/api/deps.py::resolve_identity`) calls its own
`IdentityProvider` instance, fresh, on every single tool call. If the
Agent were fully compromised and claimed "this user is DBA_MANAGER", it
would have no channel to make that claim in the first place — there is no
such field.

## Package layout

```
src/inumi/
  common/            # shared vocabulary — no service-specific logic
    models/           # failures, target, tool, risk, identity, execution contracts
    identity/         # IdentityProvider + MockIdentityProvider
    config.py          # Settings (pydantic-settings)
    service_auth.py    # signed service-to-service tokens
    observability.py   # structured logging + tracing, secret redaction

  gateway/
    domain/            # tool_registry, target_validation, authorization,
                        # policy_engine, risk_engine, approval, data_policy,
                        # rate_limiter, audit, sql_validator, tool_call_handler
    infrastructure/     # control-plane DB (SQLAlchemy models + session),
                        # execution_client (HTTP to Execution Service)
    api/                # FastAPI app + routers

  execution/
    adapters/           # DatabaseAdapter interface, SQLServerAdapter,
                        # PostgreSQLAdapter, MySQLAdapter (MySQL + MariaDB),
                        # connections (real drivers)
    discovery/           # ServerDiscoverer per platform (catalog/DMV/stats
                        # views only, never table data) + run_discovery
    credentials/        # CredentialProvider (local_dev/Vault/AWS/Azure/GCP)
    service.py           # dispatcher: ExecutionRequest -> adapter method
    api/                 # FastAPI app

  agent/
    llm/                 # base (LLMProvider), mock, anthropic/openai/gemini/
                        # deepseek providers, registry (provider+model selection)
    planner/actions.py    # structured AgentAction union
    playbooks/library.py   # fixed diagnostic sequences for known scenarios
    orchestrator.py        # investigation loop
    context_manager.py     # conversation/investigation state (in-process)
    tool_client.py          # HTTP client to the Gateway
    api/                    # FastAPI app

  channels/
    slack/               # signature verification, Block Kit rendering, sender
    teams/                # Bot Framework auth, Adaptive Cards, sender
    api/                   # FastAPI app: webhooks + /dev/chat mock channel
```

## Adding a database engine (Oracle, ...)

1. Add the platform to `common.models.target.Platform`.
2. Implement `execution.adapters.base.DatabaseAdapter` for it, using the
   engine's native diagnostics (equivalent of DMVs / `pg_stat_*`).
3. Implement `execution.discovery.base.ServerDiscoverer` for it (reads
   catalog/stats views only — never table data).
4. Register both in `execution.service._adapter_class_for` and
   `execution.discovery.engine._DISCOVERERS`.
5. Register servers of that platform in `config/servers.yaml`. Databases,
   tables, indexes and extensions are discovered automatically — they are
   never listed by hand.

Nothing in the Agent or Gateway contract changes — they only ever see the
canonical `DatabaseTarget`/`ToolDefinition`/`ExecutionRequest` models.

## Adding a channel (Discord, email, ...)

Implement a new adapter under `channels/<name>/` that verifies its own
transport's authenticity, resolves identity via the shared
`IdentityProvider`, and calls the Agent's `/v1/chat` — the same contract
Slack and Teams use. No Gateway or Agent code changes.
