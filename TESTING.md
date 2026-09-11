# Testing

## Test pyramid

```
tests/
  unit/          # policy, risk, target validation, approval, data masking,
                  # authorization, LLM registry + resilience, playbooks,
                  # adapters (via FakeQueryExecutor), SQL validator, config — no network
  integration/    # Gateway HTTP API, full tool-call pipeline, agent
                  # orchestrator, dual approval, channel webhooks — all wired
                  # via httpx.ASGITransport (in-process, no real sockets)
  security/       # the mandatory attack-scenario suite (spec §43-47)
  e2e/            # opt-in: real LLM APIs (test_live_llm) and real database
                  # engines (test_live_databases) — skipped by default
  canned_adapter.py   # deterministic DatabaseAdapter injected into the
                  # Execution Service for pipeline tests (test double, not shipped)
```

Run each layer:

```bash
make test            # unit + integration
make security-test    # security suite
make e2e              # e2e (skips everything without keys / reachable DBs)
make live-llm-test    # opt-in: hits real LLM APIs (needs a key)
./.venv/Scripts/python.exe -m pytest tests -q   # everything
```

213 tests pass (+ 24 opt-in skipped) as of this writing, with zero real
database, LLM, Slack/Teams, or secrets-manager dependency.

## Deterministic execution without "mock mode"

There is no `execution_mode` config — the shipped Execution Service always
uses real connections. Pipeline tests (which assert on
policy/risk/approval/audit behaviour, not on SQL) get determinism from an
injected `adapter_factory`: `tests/canned_adapter.py::CannedDatabaseAdapter`
is a `DatabaseAdapter` subclass returning the spec's canonical scenario
(43-session blocking chain headed by `9182`). It lives in `tests/`, never
in `src/`.

Real adapter query text is covered by `tests/unit/test_adapters.py`
(`FakeQueryExecutor`) and by `tests/e2e/test_live_databases.py` against
`docker compose up -d postgres-sample`.

## Why `httpx.ASGITransport` instead of real servers

Integration and security tests build every service's FastAPI app in-process
and wire them together with `httpx.ASGITransport`, exercising the *real*
HTTP request/response cycle (headers, status codes, JSON (de)serialization,
service-token verification) without opening a real socket or requiring
`docker compose up` first. See `tests/stack.py` for the shared four-service
stack builder.

`FastAPI`'s `lifespan` context (used for Gateway's SQLite table creation
convenience) doesn't fire under raw `ASGITransport` — tests that need it
call `await gateway_app.state.gateway.db.create_all()` explicitly, or use
`fastapi.testclient.TestClient` (which does drive lifespan) where
convenient.

## The mandatory tests (spec §44-47) and where they live

| Spec test | File |
|---|---|
| Approval mismatch (approve session 9182, execute against 9183) | `tests/unit/test_approval.py::test_approval_mismatch_when_action_changes_after_approval`, `tests/security/test_security_suite.py::test_tampered_approval_argument_mismatch_denied` |
| Expired approval denies execution | `tests/unit/test_approval.py::test_approval_expired_denies_execution`, `tests/security/test_security_suite.py::test_expired_approval_denies_execution_over_http` |
| Malicious database content is treated as data, not instructions | `tests/security/test_security_suite.py::test_malicious_database_content_is_never_obeyed` |
| Natural-language role claim ("I am DBA_L3") has no effect | `tests/security/test_security_suite.py::test_dba_role_escalation_via_message_text_has_no_effect` |
| Disabled tool → `TOOL_NOT_AVAILABLE`, no database contact | `tests/security/test_security_suite.py::test_disabled_tool_returns_tool_not_available_without_execution` |

## The acceptance scenario (spec §68)

`tests/integration/test_agent_orchestrator.py::test_acceptance_scenario_investigate_approve_execute_verify`
and `tests/integration/test_channels_api.py::test_dev_chat_full_round_trip_investigation_and_approval`
both run the full "DBA_L2 in Teams/dev-channel asks about CoreBanking →
Agent investigates via real Gateway pipeline → proposes killing the head
blocker → approval required → approved → executed → verified" scenario
end to end, across all four services.

## Writing a new security test

Prefer the shared stack builder:

```python
from tests.stack import build_stack

async def test_something():
    stack = await build_stack()  # fresh Gateway/Execution/Agent/Channels, isolated rate limits
    ...
```

Pass `rate_limit_config_path=...` to `build_stack()` if your test
legitimately needs more than the default critical-tier budget of 2
submissions/minute (see `tests/security/relaxed_rate_limits.yaml` for an
example) — don't relax the *default* limits just to make a test pass; that
usually means the test should assert fewer submissions instead.

## Opt-in live tests (`tests/e2e/`)

The default suite runs entirely against the deterministic offline planner
and the canned adapter — free, offline, reproducible. Two separate suites
exercise the real thing, skipped unless explicitly opted in:

```bash
# real LLM APIs — one provider per key you supply
RUN_LIVE_LLM_TESTS=1 ANTHROPIC_API_KEY=sk-ant-...  pytest tests/e2e/test_live_llm.py -q
RUN_LIVE_LLM_TESTS=1 OPENAI_API_KEY=sk-...         pytest tests/e2e/test_live_llm.py -q
#                    GEMINI_API_KEY / DEEPSEEK_API_KEY likewise

# real database engines
docker compose up -d postgres-sample
RUN_LIVE_DB_TESTS=1 pytest tests/e2e/test_live_databases.py -q
```

`test_live_llm.py` assertions are deliberately loose — a live model's exact
phrasing isn't stable — but do check the security-relevant property against
each real provider: it stays inside the tool menu it was offered, and
injected instruction-shaped text in a tool result (spec §45) doesn't steer
it to a destructive tool. Never wired into `make test` / CI.

## What's intentionally not covered by the default suite

- Real SQL Server/PostgreSQL connections in the *default* run — adapter
  *logic* is covered via `FakeQueryExecutor` (`tests/unit/test_adapters.py`)
  and live connections via the opt-in `tests/e2e/test_live_databases.py`.
- Real LLM API calls in the *default* run — the provider classes' SDK-specific
  plumbing is covered by the opt-in `tests/e2e/test_live_llm.py`; the
  registry / selection logic is fully covered offline in
  `tests/unit/test_llm_registry.py`.
- Real Vault/AWS/Azure/GCP secret retrieval — each provider's "not
  configured" fail-closed path is tested
  (`tests/unit/test_execution_service.py::test_without_configured_credentials_execution_fails_closed`);
  the actual SDK calls are a deployment-time integration concern.
