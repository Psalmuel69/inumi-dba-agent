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

## The no-raw-error invariant also covers discovery, not just tool calls

The "never a stack trace, never raw DB error text" rule above is about the
tool-call pipeline specifically; `/discover` (`gateway/domain/discovery.py`'s
`DiscoveryOrchestrator.refresh_all`/`refresh_server`, and the single-server
`POST /v1/catalog/refresh/{id}` route in `gateway/api/routers/catalog.py`)
is a separate path that talks to the Execution Service and can fail the same
way (an unreachable Execution Service, a 500, a timeout) — it needs the same
invariant applied on purpose, not inherited for free. A raw `httpx` exception
bakes in the request URL and, for an `HTTPStatusError`, an MDN documentation
link; `discovery.py`'s `clean_discovery_error()` maps the exception types
that matter to short DBA-facing text (status/unreachable/timeout/generic
fallback) and is the one place both call sites go through, mirroring how
`ExecutionService.execute`'s `except Exception` branch already logs the real
exception and returns a clean, generic message. The raw exception is always
still logged server-side (structlog `error_type` + `error`) — only the
DBA-facing text is generic.

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
  against the problem text. For 13 known scenarios (slow queries, high CPU,
  high memory, blocking, deadlocks, connection saturation, replication lag,
  backup health, storage capacity, transaction log reuse, error logs,
  general health, configuration tuning review), this picks a fixed, named
  sequence of
  read-only diagnostic calls. Each step of a matched playbook is submitted
  directly — **no LLM call in between** — and once the sequence completes,
  the LLM is called again to interpret everything gathered (nudged by the
  playbook's own conclusion guidance). Unmatched problem text runs fully
  freeform, unchanged.

  Reviewed against Xata's (a competing, now-archived, Postgres-only DBA
  agent) shipped playbook prompts: two gaps were worth adopting. First, its
  slow-query/high-CPU prompts explicitly exclude the engine's own
  introspection/system-catalog queries from being blamed as "the" hot
  query — `slow_queries` and `high_cpu`'s `conclusion_guidance` now carry
  the equivalent, engine-general framing. Second, its `tuneSettings`
  playbook has no analog here — `configuration_review` (get_configuration +
  get_health only) fills that gap, scoped honestly to rule-of-thumb
  misconfiguration flags rather than true capacity-based sizing, since
  Inumi's server registry has no instance-class/hardware-sizing data to
  size against.

  Checked against a separate external playbook specification: the original
  combined `storage` playbook (disk/capacity *and* transaction log growth,
  3 steps, one shared trigger list) never checked replication lag or backup
  status — two of the most common real reasons a transaction log can't
  reuse space. Split into `storage` (disk/capacity only — `get_storage`,
  `get_health`) and a new `transaction_log` playbook (`get_transaction_log`,
  `get_replication_status`, `get_backup_status`, `get_health`), each with
  its own trigger list and a `conclusion_guidance` that matches its actual
  hypothesis set — `transaction_log`'s specifically asks the model to name
  which concrete reuse blocker (active/long transaction, replication lag,
  failed/stalled log backup, log shipping/mirroring/snapshots, or
  maintenance) the evidence supports, not just report current usage.

**A playbook's fixed steps are a floor, not a ceiling.** That first
post-playbook call is *not* restricted to `conclude` — `_next_playbook_
action` returning `None` (steps exhausted) simply makes the loop fall
through to the exact same `decide_next_action` call the freeform path
uses, with the same full, unrestricted `available_tool_ids` and the same
`tool_requirements`/`tool_allowed_arguments` plumbing. `_problem_statement_
for_llm` tells the model both directions explicitly: conclude now if the
evidence already suffices, but if it doesn't, propose one or more further
read-only diagnostic tool calls — not limited to this playbook's own
steps — before concluding. Each such extra call goes through
`_submit_and_relay` exactly like any other freeform proposal (including
argument-stripping and Gateway-DENIED self-correction) and is folded into
the same transcript/evidence the eventual conclusion is built from. So "the
LLM is asked only once" is only true for a playbook whose evidence was
already sufficient — verified by a scripted-LLM test
(`tests/unit/test_playbook_freeform_extension.py`) that drives the
blocking playbook's 4 fixed steps to completion, then has the model
propose one further diagnostic outside those 4 steps before concluding,
confirming the extra call is actually submitted and its result reflected
in the final report. Crucially, this never grants extra turns: every
playbook step and every freeform extension increments the exact same
`investigation.turn_count` against the exact same `_MAX_INVESTIGATION_
TURNS`, so a long playbook simply leaves fewer freeform turns available
afterward, and a model that never converges still terminates via the
existing turn-cap/final-chance-to-conclude fallback above — pinned by
`tests/unit/test_turn_budget_playbook_plus_freeform.py`.

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

## execute() must surface the query's own result, not just rowcount

Found while live-testing the blocking playbook against a real, currently
blocking session: `database.kill_session` reported `terminated=False` for
*every* session it ever killed across this whole project's live testing —
even ones independently confirmed dead a moment later. The bug was one
layer down from the adapter: `kill_session`'s SQL is `select
pg_terminate_backend(%(pid)s) as terminated` — a SELECT, not a plain
DML/DDL statement — but every engine's `QueryExecutor.execute()`
(`execution/adapters/connections.py`) discarded the cursor's own result row
and returned only `{"rowcount": ...}`. The adapter's `result.get
("terminated", False)` then always fell back to the default. Nothing ever
raised — it was a silently wrong answer, not a visible error, so it survived
this many rounds of live testing before a live kill against a session that
was independently checked before and after finally caught it.

`execute()` on all three engines now also fetches the first returned row
(when the cursor's `description` says the statement produced one) and
merges its columns into the result dict, alongside `rowcount`. A plain
DML/DDL statement with no result columns is unaffected — `rowcount` alone.

## A write executing is not license to conclude it worked

The README's own lifecycle diagram states the property this section
enforces: `... → DATABASE → VERIFICATION → AUDIT → AI DBA → USER` — the
system verifies, then the AI DBA reports the actual outcome. A shared
external playbook spec this project follows says the same thing more
bluntly: "Never mark an incident as resolved merely because an action was
submitted. Resolution requires independent verification." Before this,
that property held only as far as the model chose to make it hold.

`ToolCallStatus.EXECUTED` on a write (e.g. `database.kill_session`) means
the Gateway/Execution pipeline ran the statement — it says nothing about
whether the condition the DBA actually cared about (a session still
blocking something) is now gone. Whether the investigation ever re-checked
that afterward (calling `database.get_blocking_sessions` again, say) was
previously left entirely to the model's own discretion within its turn
budget. Verified live, a real model has voluntarily done exactly the right
thing — "Subsequent session and blocking checks confirmed that session X
has been successfully terminated" — but nothing ever forced it to. Nothing
stopped the same model, on a different run, from proposing `kill_session`
and immediately concluding "Completed" from the bare EXECUTED status alone,
with no independent check that the session was actually gone. A DBA
reading that report has no way to tell the two cases apart.

The fix mirrors an already-established pattern in this same file:
`_ungrounded_identifiers`/`_finalize_conclude`'s existing grounding check
(see `test_conclusion_grounding.py`) already rejects a Conclude that names
something never actually seen in the investigation, and gives the model
one more bounded try. Post-remediation
verification is the same shape, applied to a different failure: a Conclude
that would report a write as done without an independent re-check.

- `orchestrator._VERIFICATION_TOOLS_BY_WRITE_TOOL` is a lookup table from a
  write tool_id to the read-only tool(s) that can cheaply, obviously
  confirm its real-world effect — currently `kill_session`/`cancel_query`
  against `get_blocking_sessions`/`get_sessions`/`get_running_queries`.
  Deliberately scoped to session-termination-shaped writes first: that's
  the case this project has repeatedly hit live and gotten wrong, and it's
  the one with an unambiguous, single-call check (does this session_id
  still show up?). A write like `update_statistics`/`create_index` has no
  equally cheap re-check (confirming it actually *helped* needs a
  follow-up performance observation, not one more tool call), so those
  intentionally stay out of the table for now — extending this to a future
  write tool with its own obvious check is one more table entry, not a new
  mechanism.
- `InvestigationState.pending_verification` is set the moment a mapped
  write executes, and cleared the moment one of its correlated read-only
  tools is itself proposed and executes — regardless of what that check
  finds. `_verification_still_shows_condition` inspects that read tool's
  own result rows for the exact session_id the write targeted (the same
  `session_id`/`blocked_session_id`/`blocking_session_id` fields the real
  adapters already return — see `execution/adapters/*.py`) and records the
  verdict on `InvestigationState.last_verification`: `"RESOLVED"` or
  `"UNRESOLVED"`.
- `_finalize_conclude` rejects a Conclude while `pending_verification` is
  still set — same self-correction shape as the grounding check: feed back
  exactly what's missing (which tool to call), append an
  `internal.verification_check` transcript entry, and let the loop's own
  turn budget bound how many times this can happen. The one exception is
  the single bounded last-chance Conclude call after the turn budget runs
  out (`final_chance=True`): it offers no tool calls at all
  (`available_tool_ids=[]`), so there is no way left for the model to
  actually go check — rejecting there would only discard everything the
  investigation found in favor of the generic no-root-cause fallback, so
  it's accepted instead, with the report saying plainly that it was never
  independently verified.
- `_format_report`/`_verification_note` state the real, structurally-
  derived outcome directly in the DBA-facing reply — never inferred from
  the model's own free-text Conclude wording, and never collapsed into one
  generic "Completed": *independently re-checked and confirmed resolved*,
  *independently re-checked and did NOT resolve*, or *executed but never
  independently checked* are three distinct, separately-worded outcomes.

Whether a tool_id is even eligible for this treatment is double-checked
against the tool catalog's own `operation_type` (already returned in full
by `/v1/tools` — nothing new had to be threaded through the Gateway for
this), the same belt-and-suspenders reasoning
`_strip_unschematized_arguments` already uses: `_VERIFICATION_TOOLS_BY_
WRITE_TOOL`'s own tool_id membership is the primary signal, but if the
catalog no longer classifies that tool_id as `OperationType.WRITE`, this
mechanism stands down rather than trusting a static mapping that might now
be stale.

Verified via `tests/unit/test_post_remediation_verification.py`, including
a scripted reproduction of the exact live pattern: a mock LLM proposes
`kill_session`, gets `EXECUTED` (`terminated=True`), then immediately
proposes `Conclude` claiming success with no re-check at all — rejected,
not accepted at face value — alongside the same scenario with a proper
re-check afterward (accepted, reply states it was independently verified),
one where the re-check shows the session is still there (accepted, reply
states it was NOT resolved), and one where the model never re-checks at
all before the turn budget runs out (accepted only via the bounded
last-chance path, reply states it was never independently verified).

## A NoArgs tool's own required-arguments entry must say "[]", not nothing

Observed repeatedly across live Slack testing: `database.get_blocking_sessions`
and `database.get_sessions` — both `NoArgs` in `gateway/domain/tool_catalog.py`
(`ARGUMENT_MODELS`, `extra="forbid"`) — kept getting proposed with an extra
`reason` (sometimes `session_id` or `database_name`) folded into `arguments`.
The Gateway correctly rejected each one as `INVALID_ARGUMENTS`, and the
orchestrator's self-correction path (`_SELF_CORRECTABLE_DENIAL_CODES`) always
recovered within the same turn — never broke an investigation — but it
wasted an LLM call and a Gateway round-trip every single time it happened.

The root cause was in `orchestrator._continue_investigation`, not the model:
`tool_requirements` (the per-tool required-argument map injected into
`decide_next_action`'s prompt, see `StructuredLLMProvider._ACTION_SYSTEM`)
was built as `{tool_id: reqs for t in available if (reqs := ...required)}` —
a tool with an empty required list (every `NoArgs` read) was walrus-filtered
out of the dict entirely, not included with an empty list. The model was
never actually told "this tool takes nothing"; it only ever saw entries for
tools that DO need something, and reasonably (if wrongly) generalized from
the `arguments` schema's superset of real properties (`session_id`, `reason`,
... — genuine requirements for *other* tools) that the prompt has to declare
up front for the reasons in `llm/base.py`'s own comment on `_FLAT_ACTION_SCHEMA`
(a property-less object schema gives a weaker model nothing to fill in).
`ProposeToolCall` already has its own top-level `reason` for the
human-readable justification — the model was folding that same idea into
`arguments` a second time, unprompted, for tools whose real schema has no
room for it at all.

The fix has two layers, deliberately not just one:

1. **Prompt fix** (root cause): `tool_requirements` now includes every
   available tool, NoArgs ones mapped to `[]` — an explicit "this tool needs
   nothing" signal instead of silence the model has to interpret on its own.
   `_ACTION_SYSTEM` and `_FLAT_ACTION_SCHEMA`'s `arguments` description both
   spell out what an empty list means and explicitly forbid borrowing a
   property that belongs to some *other* tool.
2. **Defense-in-depth** (`orchestrator._strip_unschematized_arguments`,
   called from `_submit_and_relay`): even if a prompt fix doesn't hold for
   every provider/model forever, any `arguments` key outside a tool's own
   real property set is silently dropped before a `ToolCallRequest` is ever
   built — the Gateway never sees it, so there is nothing left to deny.
   Dropping such a key is always safe: it could never have been one that
   tool's own schema would have accepted.

Verified live via the real orchestrator + real Gateway pipeline (in-process
ASGI, `tests/integration/test_no_args_extra_arguments.py`): a scripted
planner reproducing the exact bad completion above now completes on its
first attempt, with zero `INVALID_ARGUMENTS` denials in the transcript.

## A known environment must survive across investigations, not just within one

Live-reproduced in a real Slack thread: the DBA established development/
postgres-local early in a conversation, ran two more successful
investigations that each named postgres-local again, and then sent a plain
follow-up ("So what database is the copy activity happening on?") that
named neither an environment nor a server. `handle_message` (`agent/
orchestrator.py`) wrongly re-asked "which environment should I
investigate?" even though the environment had already been established
earlier in this exact conversation.

`state.database_context` (`agent/context_manager.py::ConversationState`) is
the actual source of truth here — it's a `ConversationState` field, so it
outlives any one `InvestigationState` and is never reset when an
investigation concludes and a new one starts. A brand-new investigation's
environment gate (`if "environment" not in state.database_context: ask`)
already reads this persisted value, not just the current message's
`intent.environment_hint` — so once established, it was never actually at
risk of being forgotten by that check alone. The real gap was the missing
other half of the *instance* symmetry already in place for `database`: a
switch to a different, named instance correctly pops a stale `database`
(it might not exist on the new server) via `state.database_context.pop
("database", None)`, but nothing equivalent protected `environment` — if
that new instance was unregistered or ambiguous (`_environment_for_instance`
returns `None`), the OLD instance's environment silently kept asserting
itself for the new one instead of being dropped, which is exactly the kind
of guess the spec forbids ("for production targets I won't guess"). Fixed
by popping `state.database_context["environment"]` too whenever a genuine
instance switch's environment can't be auto-resolved — mirroring the
existing database-goes-stale-on-switch logic exactly — and by making the
final "not in state.database_context" check's rationale explicit in code
comments, so it can't be quietly narrowed to `intent.environment_hint`
alone by a future change. See the two new cases in
`tests/unit/test_environment_clarification.py` for the exact scenario
pinned: a later fresh investigation never re-asking for an
already-known environment, and switching to an unresolvable instance
correctly forgetting the stale one instead of guessing.

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
