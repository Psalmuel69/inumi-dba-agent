# Policy Model

## Decisions

The Policy Engine (`gateway/domain/policy_engine.py`) returns exactly one
of three decisions for a given `(environment, tool, role)`:

- **ALLOW** — proceed to risk assessment and execution directly.
- **DENY** — refuse. Nothing about the request reaches the Execution Service.
- **REQUIRES_APPROVAL** — an `Approval` must exist and be granted (spec §15)
  before execution proceeds.

**Unlisted combinations always evaluate to `DENY`** (`default_decision: DENY`
in `config/policy.yaml`) — adding a new tool or role without a policy entry
fails closed, not open.

## Configuration

`config/policy.yaml`:

```yaml
default_decision: DENY

environments:
  production:
    database.kill_session:
      DBA_L1: DENY
      DBA_L2: REQUIRES_APPROVAL
      DBA_L3: REQUIRES_APPROVAL
      DBA_MANAGER: REQUIRES_APPROVAL

change_ticket_required:
  production:
    - database.restart_instance

dual_approval_required:
  - database.restart_instance
  - database.failover
```

Business rules live **only** here (and in `ToolDefinition.requires_dual_approval`
as a tool-intrinsic floor) — never in an agent prompt.

## Escalation rules layered on top of the static table

`PolicyEngine.evaluate` can escalate (never de-escalate) a base `ALLOW` to
`REQUIRES_APPROVAL`:

1. **Outside maintenance window** — if the tool has `availability_impact=true`
   and the target database's `maintenance_window` (from
   `config/servers.yaml`, timezone-aware) doesn't currently cover "now."
2. **Current load is critical** — if `current_load_critical=True` is passed
   in (a hook for future integration with a live health check) and the tool
   is a write operation.

`change_ticket_required` is evaluated independently: if a tool requires a
change ticket in this environment and no `change_id` was supplied, the
Gateway raises `CHANGE_TICKET_REQUIRED` regardless of what the base table
said (see `tool_call_handler.py` step 6).

## Risk

`gateway/domain/risk_engine.py::RiskEngine.assess` computes a `RiskAssessment`
independent of policy — policy decides *whether* an action needs approval;
risk decides *how it should be described* (risk level, score, blast radius,
reversibility, reason codes) to the approver and in the audit trail.

Scoring starts from the tool's declared `risk_level` floor and adds points
for: production environment, database criticality, write/availability
impact, irreversibility, and current load — but only for operations that
can actually change state (a pure read never gets penalized for running
against a critical production database, since its blast radius is
identical everywhere).

Blast radius (`common/models/risk.py::BlastRadius`) is looked up per tool
id (`risk_engine.py::_TOOL_BLAST_RADIUS`) and escalated to
`MULTIPLE_OBJECTS` if the caller indicates more than one affected object.

## Approval

See [SECURITY.md](SECURITY.md) rule #10/#11 and
[THREAT_MODEL.md](THREAT_MODEL.md) threat #5 for the approval binding and
expiry model in depth. In short: `ApprovalContext.action_hash()` binds
actor + tool + tool version + target + normalized arguments + environment +
database + risk level; any change to the underlying request invalidates the
approval when re-verified at execution time.

## Maintenance windows

```yaml
maintenance_window:
  timezone: Africa/Lagos
  start: "01:00"
  end: "04:00"
```

`gateway/domain/policy_engine.py::is_within_maintenance_window` handles
windows that wrap midnight and falls back to "no restriction" if a database
has no window configured. **The LLM never declares a maintenance window
active** — this is computed purely from configuration and wall-clock time.

## Rate limiting

`config/rate_limits.yaml` defines three tiers (`read`, `write`, `critical`),
each with five independent budgets (per user, per conversation, per tool,
per database, per environment), enforced in-memory for local dev/tests
(`InMemoryRateLimitBackend`) or via Redis in production
(`RedisRateLimitBackend`). A `WRITE`/`PRIVILEGED` operation, or any
operation requiring approval, is billed against the stricter `critical`
tier.

## LLM selection

Which LLM the agent uses is a *product* choice, not a security control —
`agent/llm/registry.py::LLMRegistry` resolves it, and it never touches
authorization, policy, risk, or approval (all of which live in the Gateway
and are identical regardless of provider).

**Configuration** (`Settings`, see `.env.example`):

| Setting | Effect |
|---|---|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` / `DEEPSEEK_API_KEY` | A provider becomes *selectable* the moment its key is present. |
| `LLM_PROVIDER` = `""` / `auto` | Auto-pick the first configured provider (order: anthropic, openai, gemini, deepseek). DBAs may switch per-conversation. |
| `LLM_PROVIDER` = `<name>` | Force that provider **and disable** per-conversation switching. |
| `LLM_PROVIDER` = `mock` | Force the deterministic offline planner. |
| `LLM_MODEL` | Default model for the resolved provider (`""` = the provider's own default). |
| `ALLOW_USER_MODEL_SELECTION` = `false` | Keep the auto default but disable the `/model` command. |

With no key set at all, `effective_default_llm()` returns `("mock", …)` —
and `validate_for_production()` refuses to start the process in that state
when `INUMI_ENV=production`.

**Resilience.** A provider outage or malformed completion never crashes the
chat or hangs it indefinitely — it degrades to a clear message. Each
`decide_next_action`/`extract_intent` call gets one same-model retry
(`StructuredLLMProvider._CALL_RETRIES`), and the Gemini provider
additionally switches to the next model in a ranked fallback chain on a
quota or capacity error specific to the current model (cooling that model
down rather than blacklisting it permanently — see `_cooldown_seconds`,
which prefers the API's own `retryDelay` hint). Whatever combination of
retries and model switches happens underneath, one call is never worse than
`StructuredLLMProvider._OVERALL_DEADLINE_SECONDS` (20s) late — see
[ARCHITECTURE.md](ARCHITECTURE.md#latency-ceiling-on-a-single-llm-decision).

**Per-conversation selection** (chat commands, `agent/orchestrator.py`):

- `/models` — lists each configured provider and the models its key can
  actually use (a live `models.list()` call).
- `/model` — shows the current selection and its source (conversation vs
  deployment default).
- `/model <provider> <model>` — switches for this conversation. Rejected if
  the provider isn't configured, the model isn't in that key's list, or
  selection is locked/disabled.

The selection is stored on `ConversationState` (in Agent process memory)
and is never an input to any Gateway decision.
