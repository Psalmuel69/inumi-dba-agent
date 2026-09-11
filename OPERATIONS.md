# Operations

## Day-to-day

- **Adding a DBA:** add them to the real enterprise IdP group(s) referenced
  in `config/identity.yaml`'s `identity.groups`/`identity.roles` — nothing
  in this codebase needs to change or redeploy.
- **Onboarding a server:** add an entry to `config/servers.yaml` with its
  host/port, environment, criticality, `allowed_roles`, maintenance window,
  and any per-database overrides, then store its credential in the secrets
  manager under the same server id. The Gateway discovers the databases,
  tables, indexes and extensions on that server automatically (on first use,
  on `/discover`, or eagerly at startup with
  `INUMI_DISCOVERY_ON_STARTUP=true`); individual databases are never
  registered by hand. The Agent selects targets from the registry + the
  discovered catalog, never from a freehand connection string (spec §11).
  Discovery reads catalog and statistics views only — never table contents.
- **Changing what requires approval:** edit `config/policy.yaml`. Changes
  take effect on the Gateway's next restart (config is loaded once at
  process start — restart the Gateway deployment to pick up policy edits).
- **Enabling a restricted tool:** flip the corresponding `ENABLE_*_TOOL`
  environment variable for the Gateway *and* Execution Service, and confirm
  an adapter implementation actually exists for it (see
  [TOOL_CATALOG.md](TOOL_CATALOG.md) — several restricted tools have an
  argument schema but deliberately no execution path yet).
- **Adding/changing a playbook:** edit `agent/playbooks/library.py` —
  each entry is a `playbook_id`, trigger phrases (matched word-boundary,
  case-insensitive), a fixed list of read-only diagnostic steps, and
  conclusion guidance for the LLM's final call. Restart the Agent to pick
  up changes (loaded once at import time, like the tool catalog). See
  [ARCHITECTURE.md](ARCHITECTURE.md#investigation-loop-freeform-vs-playbook-driven).
  Ask `/playbooks` in chat to see what's currently registered.

## Monitoring what matters

Per spec §42, track (via the OpenTelemetry wiring in
`common/observability.py` and the audit/security-event tables):

- Policy denials and security events (`security_events` table) — alert on
  spikes, they're the leading indicator of probing/misuse.
- Approval latency (time between `APPROVAL_REQUESTED` and
  `APPROVED`/`REJECTED`/`EXPIRED` audit events) — a consistently-expiring
  approval queue means DBAs aren't seeing the approval cards in time.
- Execution failures and timeouts, by tool and by database — a rising
  `EXECUTION_TIMEOUT` rate on one instance is itself an incident signal.
- Rate-limit rejections — sustained hits usually mean either abuse or a
  legitimately-busy incident response that needs a temporary limit bump
  (edit `config/rate_limits.yaml` and redeploy the Gateway).
- `llm_call_deadline_exceeded` (structured log, Agent) — a single decision
  hit the ~20s hard ceiling; sustained occurrences mean the configured
  provider is degraded/exhausted across its whole fallback chain, not a
  one-off blip. `gemini_model_unavailable_switching` shows which model and
  why (quota vs. capacity) leading up to it.

## Approval queue hygiene

Approvals expire (default 10 minutes; see `ApprovalEngine`'s
`default_ttl_seconds`) and are never resurrected — an expired request must
be re-proposed by the Agent from scratch, which re-runs the full
policy/risk pipeline against current state. This is intentional: stale
context (a blocking chain that resolved itself, a database that's since
gone into maintenance) should not authorize a now-irrelevant action.

## Investigation/audit retention

Per spec §59, retention for audit logs, chat messages, investigations, and
execution results should be configured according to your organization's
compliance requirements; this repo does not hard-code a retention job, but
every table involved (`audit_events`, `security_events`,
`tool_executions`, `investigations`) is a normal table an operational
retention job can prune by `created_at`. Prefer storing hashes/references
over raw sensitive result data where retention windows are long — the
`arguments_hash` field on `audit_events` already follows this pattern.

## Runbook: a tool call is stuck in `APPROVAL_REQUIRED`

1. `GET /v1/approvals/{approval_id}` to check status/expiry.
2. If it's expired, there's nothing to approve anymore — ask the DBA to
   re-issue the request through the Agent.
3. If it's `AWAITING_SECOND_APPROVAL`, a second, *different*, independently
   authorized approver needs to approve — check `config/policy.yaml`'s
   `dual_approval_required` list and the tool's `allowed_roles` to confirm
   who's eligible.

## Runbook: a DBA reports "I approved it but nothing happened"

Check the audit trail for that `approval_id`/`request_id`:
- `APPROVAL_MISMATCH` means the resubmitted action didn't match what was
  approved — the underlying request must have changed between proposal and
  approval (see [THREAT_MODEL.md](THREAT_MODEL.md) threat #5).
- `APPROVAL_INVALID` with "already been used" means it already executed
  once — check `tool_executions` for the prior run rather than re-approving.
