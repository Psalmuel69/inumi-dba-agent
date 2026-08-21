# Testing

## Test pyramid

```
tests/
  unit/          # policy, risk, target validation, approval, data masking,
                  # authorization, adapters (via FakeQueryExecutor), SQL
                  # validator, execution service dispatch — no network, no DB
  integration/    # Gateway HTTP API, full tool-call pipeline, agent
                  # orchestrator, dual approval, channel webhooks — all wired
                  # via httpx.ASGITransport (in-process, no real sockets)
  security/       # the mandatory attack-scenario suite (spec §43-47)
  e2e/            # (reserved) tests exercising a fully deployed stack
```

Run each layer:

```bash
make test            # unit + integration
make security-test    # security suite
make e2e              # e2e (once populated for your deployment)
./.venv/Scripts/python.exe -m pytest tests -q   # everything
```

92 tests pass as of this writing with zero real database or LLM dependency.

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

## Opt-in live-Claude smoke tests (`tests/e2e/test_live_anthropic.py`)

The default suite runs entirely against `MockLLMProvider` — deterministic,
free, offline. A small, separate set of tests exercises the *real*
Anthropic API instead, but only when explicitly asked for:

```bash
make live-llm-test   # or: RUN_LIVE_LLM_TESTS=1 ANTHROPIC_API_KEY=sk-ant-... pytest tests/e2e -q
```

They're skipped by default (no env var, no key needed to run `pytest`) and
their assertions are deliberately loose — a live model's exact phrasing
isn't guaranteed stable between runs. What they *do* check is the property
that actually matters for security against a real model rather than the
deterministic stand-in: it never proposes a `tool_id` outside the list it
was offered, and injected instruction-shaped text in a tool result
(spec §45) doesn't make it prefer a destructive tool over a safe one.
Never wired into `make test`/`test-all` or CI — see
`common.config.Settings.validate_for_production` for the mechanism that
guarantees a real deployment can't accidentally run on the mock provider
in the first place (that's a startup check, not a test).

## What's intentionally not covered by automated tests

- Real SQL Server/PostgreSQL connections (`execution/adapters/connections.py`)
  — these require live infrastructure; adapter *logic* (which DMV/pg_stat
  query maps to which tool) is fully covered via `FakeQueryExecutor`
  instead (`tests/unit/test_adapters.py`).
- Real Vault/AWS/Azure/GCP secret retrieval — each provider's "not
  configured" fail-closed path is tested
  (`tests/unit/test_execution_service.py::test_real_mode_without_configured_credentials_fails_closed`);
  the actual SDK calls are a deployment-time integration concern.
