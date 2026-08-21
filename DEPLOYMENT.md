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

## Execution mode

Set `EXECUTION_MODE=real` and install the `db-drivers` extra
(`pip install -e ".[db-drivers]"`, or add it to the execution service's
Docker build) once real SQL Server/PostgreSQL instances and credentials are
available. Leaving it on `mock` in production would be a fail-*open*
posture and should be treated as a deployment-blocking misconfiguration —
consider asserting `settings.execution_mode == "real"` in your production
startup checks.

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
