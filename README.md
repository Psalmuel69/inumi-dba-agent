# Inumi — Enterprise AI DBA Agent & Secure DBA Control Gateway

**Inumi** (`@Inumi`) is an AI database administration assistant that DBA teams talk
to over Slack and Microsoft Teams. It investigates incidents, analyzes
performance, and — only through a fully independent, non-bypassable **DBA
Control Gateway** — executes approved remediation.

The single most important property of this system:

```
USER → VERIFIED IDENTITY → AI DBA → UNTRUSTED TOOL REQUEST → DBA CONTROL GATEWAY
     → AUTHORIZATION → POLICY → RISK → APPROVAL (if required) → SCOPED EXECUTION
     → DATABASE → VERIFICATION → AUDIT → AI DBA → USER
```

**The AI agent never holds a database credential, never has direct network
access to a database, and never determines its own authorization.** See
[ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY.md](SECURITY.md), and
[THREAT_MODEL.md](THREAT_MODEL.md) for how that's enforced structurally, not
by prompting.

## Services

| Service       | Responsibility                                                                 |
|---------------|----------------------------------------------------------------------------------|
| `channels`    | Slack + Microsoft Teams webhook adapters. Verifies signatures/tokens, resolves identity, forwards to `agent`. Never touches a database. |
| `agent`       | The LLM-backed investigation/planning loop (Anthropic / OpenAI / Gemini / DeepSeek, DBA-selectable per conversation, or a deterministic offline planner). Proposes tool calls; has no DB credential and no authorization authority. For a recognized scenario (slow queries, high CPU, blocking, ...) it follows a fixed, named [playbook](ARCHITECTURE.md#investigation-loop-freeform-vs-playbook-driven) instead of investigating fully freeform. |
| `gateway`     | **The security boundary.** Tool registry, target validation, authorization, policy, risk, approval, data minimization, rate limiting, audit. |
| `execution`   | The only service with database credentials/network access. Dispatches to `SQLServerAdapter`/`PostgreSQLAdapter`/`MySQLAdapter` (MySQL + MariaDB). |

Supporting infrastructure: PostgreSQL (control-plane database), Redis
(rate limiting in production).

## Quick start (local development)

The test suite needs no external services. Running the system needs a
database to point at (bundled sample below) and, optionally, an LLM key.

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
cp .env.example .env
cp config/dev_credentials.example.yaml config/dev_credentials.yaml
./.venv/Scripts/python.exe -m pytest        # 226 pass, 24 opt-in skipped
```

Run the full stack (bundled sample PostgreSQL target included):

```bash
docker compose up --build
```

Talk to it via the dev channel (no real Slack/Teams needed, spec §66):

```bash
curl -X POST http://localhost:8003/dev/chat \
  -H "Content-Type: application/json" \
  -d '{"user": "dba_l2@example.com", "message": "Check blocking on the production PostgreSQL cluster."}'
```

**Registering databases.** You don't. Register each *server* in
`config/servers.yaml` (host, environment, criticality, allowed roles,
maintenance window, per-database overrides) and store its diagnostic
credential in the secrets manager under the same id. Inumi discovers the
databases, tables, indexes and extensions on that server itself — reading
catalog and statistics views only, never table or view contents. Ask it
`/servers`, `/catalog <server>`, or `/discover` in chat.

**Choosing an LLM.** Set a key for any of `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` (e.g.
`ANTHROPIC_API_KEY=sk-ant-... docker compose up`). Each configured provider
becomes selectable in chat with `/models` and `/model <provider> <model>`.
With no key set, the agent uses a deterministic offline planner. A single
investigative decision is never worse than ~20s late no matter how many
retries or model fallbacks happen underneath — see
[POLICY_MODEL.md](POLICY_MODEL.md#llm-selection).

**Playbooks.** For a recognized scenario (slow queries, high CPU, blocking,
deadlocks, replication lag, connection saturation, ...) the agent follows a
fixed, named diagnostic sequence instead of deciding each step freeform —
faster and more consistent for the handful of situations that come up over
and over. Ask `/playbooks` in chat to see the current list, or read
[ARCHITECTURE.md](ARCHITECTURE.md#investigation-loop-freeform-vs-playbook-driven)
for how and why.

See [DEVELOPMENT.md](DEVELOPMENT.md) for the full local setup and
[TOOL_CATALOG.md](TOOL_CATALOG.md) for what `@Inumi` can currently do.

## Commands

```bash
make dev             # create venv, install, copy .env.example
make test            # unit + integration tests
make security-test   # dedicated security test suite (spec §43-47)
make e2e             # end-to-end scenario tests
make lint            # ruff + mypy
make docker-up       # full stack via docker compose
```

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — service boundaries, data flow, why the Gateway is the only path to a database
- [SECURITY.md](SECURITY.md) — the security model and where each control actually lives
- [THREAT_MODEL.md](THREAT_MODEL.md) — threats, attack paths, mitigations, residual risk, tests
- [API.md](API.md) — REST API reference for all four services
- [TOOL_CATALOG.md](TOOL_CATALOG.md) — every tool, its risk classification, and whether it's enabled by default
- [POLICY_MODEL.md](POLICY_MODEL.md) — how policy/risk/approval decisions are made and configured
- [DATABASE_ADAPTERS.md](DATABASE_ADAPTERS.md) — SQL Server/PostgreSQL adapter design, adding a new engine
- [DEPLOYMENT.md](DEPLOYMENT.md) — production deployment guidance
- [OPERATIONS.md](OPERATIONS.md) — running it day to day
- [DEVELOPMENT.md](DEVELOPMENT.md) — local dev setup, project layout, coding standards
- [TESTING.md](TESTING.md) — test pyramid and how to run each layer
- [INCIDENT_RESPONSE.md](INCIDENT_RESPONSE.md) — what to do if Inumi itself is the incident

## Status

Phases 1–9 of the build (foundation → gateway → execution → read tools →
agent → channels → approvals → controlled writes → restricted-tool
framework), server registration + discovery, and MySQL/MariaDB adapters are
implemented and covered by an automated test suite (226 passing + 24
opt-in: unit, integration, and a dedicated security suite). The Execution
Service uses real database connections against SQL Server, PostgreSQL,
MySQL, and MariaDB; the Agent supports Anthropic, OpenAI, Gemini, and
DeepSeek with per-conversation model selection, bounded-latency resilience
(retry → model fallback → a hard ~20s ceiling on any single decision, so a
provider outage degrades to a clear message in seconds, never a multi-minute
hang), and, for a recognized scenario, playbook-driven investigation (see
[ARCHITECTURE.md](ARCHITECTURE.md#investigation-loop-freeform-vs-playbook-driven)).
An Oracle adapter, a real OIDC identity provider, and the real
Vault/AWS/Azure/GCP secrets-manager SDK calls are structured for but not yet
implemented — see the "Extending" / "Adding" sections in
[DATABASE_ADAPTERS.md](DATABASE_ADAPTERS.md), [ARCHITECTURE.md](ARCHITECTURE.md),
and [DEPLOYMENT.md](DEPLOYMENT.md).
