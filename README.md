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
| `agent`       | The LLM-backed investigation/planning loop. Proposes tool calls; has no DB credential and no authorization authority. |
| `gateway`     | **The security boundary.** Tool registry, target validation, authorization, policy, risk, approval, data minimization, rate limiting, audit. |
| `execution`   | The only service with database credentials/network access. Dispatches to `SQLServerAdapter`/`PostgreSQLAdapter`. |

Supporting infrastructure: PostgreSQL (control-plane database), Redis
(rate limiting in production).

## Quick start (local development)

No real Slack/Teams/SQL Server/PostgreSQL credentials are required — the
system runs fully in "mock" mode out of the box.

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
cp .env.example .env
./.venv/Scripts/python.exe -m pytest
```

Run the full stack with Docker Compose (still mock-mode by default):

```bash
docker compose up --build
```

Then talk to it without any real chat platform, via the mock dev channel
(spec §66):

```bash
curl -X POST http://localhost:8003/dev/chat \
  -H "Content-Type: application/json" \
  -d '{"user": "dba_l2@example.com", "message": "Check blocking on CoreBanking production."}'
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for the full local setup (including how
to point the system at a real Slack/Teams app or a real database engine),
and [TOOL_CATALOG.md](TOOL_CATALOG.md) for what `@Inumi` can currently do.

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
framework) are implemented and covered by an automated test suite (92
tests as of this writing: unit, integration, and a dedicated security
suite). Oracle/MariaDB adapters and a real OIDC identity provider are
structured for but not yet implemented — see the "Extending" sections in
[DATABASE_ADAPTERS.md](DATABASE_ADAPTERS.md) and
[ARCHITECTURE.md](ARCHITECTURE.md).
