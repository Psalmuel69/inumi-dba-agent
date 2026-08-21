# Development

## Setup

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
cp .env.example .env
./.venv/Scripts/python.exe -m pytest
```

No real database, Slack/Teams app, or LLM API key is required — defaults
are `EXECUTION_MODE=mock`, `LLM_PROVIDER=mock`, `IDENTITY_PROVIDER=mock`.

To exercise a real database engine locally, install the driver extra and
flip the mode:

```bash
./.venv/Scripts/python.exe -m pip install -e ".[db-drivers]"
# then set EXECUTION_MODE=real and fill in config/dev_credentials.yaml
```

To exercise the real Anthropic-backed planner instead of the deterministic
mock one, set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` in `.env`.

## Project layout

See "Package layout" in [ARCHITECTURE.md](ARCHITECTURE.md). In short:
`common/` has no service-specific logic, `gateway/domain/` is the security
boundary's business logic, `gateway/api/` and its equivalents in
`execution/`/`agent/`/`channels/` are thin FastAPI wiring around it.

## Coding standards

- Python 3.12+, full type hints, Pydantic v2 models for every boundary
  (never a bare `dict` crossing a trust boundary).
- Business logic lives in `domain/` modules, not in FastAPI route handlers
  — routers in `api/routers/*.py` should stay a thin translation layer
  between HTTP and a domain call.
- Every `InumiError` carries a `FailureCode` (`common/models/failures.py`)
  and a safe, user-facing `detail` — never let a raw exception message or
  stack trace reach a channel adapter.
- New tools: see the "Adding a new tool" section of
  [TOOL_CATALOG.md](TOOL_CATALOG.md).
- Run `make lint` (ruff + mypy) before committing.

## Running a single service locally

```bash
make run-execution   # port 8002
make run-gateway     # port 8001
make run-agent       # port 8000
make run-channels    # port 8003
```

Each command runs against `sqlite+aiosqlite:///./inumi_dev.db` by default
(see `.env.example`); for a closer-to-production setup, run
`docker compose up postgres redis` first and point `CONTROL_DB_URL` at it.

## Local end-to-end smoke test without any UI

```bash
curl -X POST http://localhost:8003/dev/chat \
  -H "Content-Type: application/json" \
  -d '{"user": "dba_l2@example.com", "message": "CoreBanking production is slow. Investigate."}'
```

Approve the resulting action:

```bash
curl -X POST http://localhost:8003/dev/chat/events \
  -H "Content-Type: application/json" \
  -d '{"user": "dba_l2@example.com", "conversation_id": "dev-conversation", "approval_id": "<from previous response>", "decision": "approve"}'
```

## Mock users (development only — see `config/identity.yaml`)

| dev user | Slack account id | Teams AAD id | Role |
|---|---|---|---|
| `dba_l1@example.com` | `U_MOCK_L1` | `aad-mock-l1` | DBA_L1 |
| `dba_l2@example.com` | `U_MOCK_L2` | `aad-mock-l2` | DBA_L2 |
| `dba_l3@example.com` | `U_MOCK_L3` | `aad-mock-l3` | DBA_L3 |
| `dba_l3b@example.com` | `U_MOCK_L3B` | `aad-mock-l3b` | DBA_L3 (second, for dual-approval testing) |
| `dba_manager@example.com` | `U_MOCK_MGR` | `aad-mock-mgr` | DBA_L3 + DBA_MANAGER |
| `notadba@example.com` | `U_MOCK_NONDBA` | `aad-mock-nondba` | not a DBA |

These are fictitious accounts for local development/tests only.
