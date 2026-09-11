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
