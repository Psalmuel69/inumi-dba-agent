# Deployment

## Network topology (spec §30)

```
Internet
   │
   ▼
Slack / Teams  (public, managed by Slack/Microsoft)
   │
   ▼
Ingress (TLS-terminating load balancer)
   │
   ▼
Channels  ──▶  Agent
                 │
                 ▼
              Gateway
                 │
                 ▼
           Execution Service
                 │
                 ▼
      Private Database Network  (no public route)
```

- **Only** the Execution Service has a network route into the private
  database network/subnet. The Agent and Channels services should not even
  have security-group/firewall rules permitting egress to database ports.
- The Gateway does not need — and should not be granted — network access to
  databases either; it only talks to the Execution Service over HTTP.
- Put the Gateway and Execution Service behind internal-only load
  balancers/service mesh; only Channels (and, if used directly, Agent)
  needs any internet-facing surface, and only for the Slack/Teams webhook
  paths.

## Service-to-service auth in production

`SERVICE_JWT_SECRET` in this repo is an HMAC shared secret suitable for a
single-cluster deployment behind a private network. For a stronger
posture, replace `common/service_auth.py`'s implementation with mTLS
(e.g., via a service mesh) or OIDC client-credentials between services —
every call site already goes through this one module, so the blast radius
of that change is contained.

## Identity provider

Implement `common.identity.IdentityProvider` against your real enterprise
IdP (Azure AD / Okta / Ping / ...) and set `IDENTITY_PROVIDER` accordingly;
`gateway/api/state.py::_build_identity_provider` is the single place that
needs to construct it. Never ship `MockIdentityProvider` to production —
it is deliberately config-driven and would need a real, secret-bearing
directory to be dangerous, but it is not an authentication mechanism.

## Secrets manager

Set `SECRETS_PROVIDER` to `vault`, `aws_secrets_manager`,
`azure_key_vault`, or `gcp_secret_manager` and fill in the corresponding
connection details. Each `*CredentialProvider` in
`execution/credentials/provider.py` currently fails closed with a clear
error until its SDK integration is completed — intentionally, so a
misconfiguration cannot silently fall back to a less-secure credential
source (spec §63). Wiring in the real SDK calls is the last step before a
production cutover.

## Database schema migrations

```bash
CONTROL_DB_URL=postgresql+asyncpg://... alembic upgrade head
```

`migrations/versions/0001_initial_schema.py` builds every control-plane
table directly from the SQLAlchemy models (`gateway/infrastructure/db/models.py`)
— add new revisions with `alembic revision` for subsequent schema changes.

## Databases

The Execution Service always uses real connections. `pyodbc` and `psycopg`
are part of the base install; `pyodbc` needs the platform ODBC driver at
runtime (the `Dockerfile` installs Microsoft ODBC Driver 18). Provide
connection details via `SECRETS_PROVIDER` (a real secrets manager in
production) or, for dev, `config/dev_credentials.yaml`.

## LLM providers

Set an API key for each provider you want DBAs to be able to use:
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`.

- `LLM_PROVIDER` unset / `auto` — the first configured provider (order:
  anthropic, openai, gemini, deepseek) is the default; DBAs may switch
  per-conversation with `/model`.
- `LLM_PROVIDER=<name>` — force that provider and **disable** per-conversation
  switching (locked deployment).
- `ALLOW_USER_MODEL_SELECTION=false` — keep auto-default but disable `/model`.
- With no key set, the agent uses the deterministic offline planner —
  which `validate_for_production()` rejects at startup in production.

Model lists shown by `/models` come from a live `models.list()` call
against the provider using the configured key, so DBAs only ever see models
that key can actually use.

## Production startup checks (fail closed, enforced — not just documented)

Every service's `create_app()` calls `Settings.validate_for_production()`
(`common/config.py`) before it does anything else. With `INUMI_ENV=production`,
the process **refuses to start** — raises `RuntimeError` immediately,
rather than serving traffic in a weakened mode — if any of the following
development-only defaults are still set:

- no real LLM provider key is configured (or `LLM_PROVIDER` points at a
  provider whose key is missing)
- `IDENTITY_PROVIDER=mock` (must be a real enterprise identity provider)
- `SECRETS_PROVIDER=local_dev` (must be `vault`/`aws_secrets_manager`/`azure_key_vault`/`gcp_secret_manager`)
- `SERVICE_JWT_SECRET` still equal to the shipped development placeholder

This is deliberately a hard crash-on-boot, not a warning log — an operator
who forgets to flip one of these in `INUMI_ENV=development` (the default)
is unaffected either way; setting `INUMI_ENV=production` is the trigger.
See `tests/unit/test_config.py` for the coverage of this check.

## Observability

`common/observability.py` wires structured JSON logging and an OpenTelemetry
`TracerProvider` per service. Point `console_export` at a real OTLP
exporter for production (swap `ConsoleSpanExporter` for
`OTLPSpanExporter`). Never log secrets or raw database rows — see
[SECURITY.md](SECURITY.md).

## Rate limiting backend

Switch `RateLimiter`'s backend from `InMemoryRateLimitBackend` to
`RedisRateLimitBackend` (`gateway/domain/rate_limiter.py`) for any
multi-instance Gateway deployment — the in-memory backend's counters are
per-process and would under-count across replicas.

## Container images

`Dockerfile` builds one image; `SERVICE` build/run arg selects which
FastAPI app it serves (`gateway`/`execution`/`agent`/`channels`). See
`docker-compose.yml` for a full local topology including PostgreSQL and
Redis, and as a starting point for a Kubernetes/ECS manifest per service.
