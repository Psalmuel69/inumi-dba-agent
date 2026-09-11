# API Reference

All four services are FastAPI apps; each exposes interactive OpenAPI docs at
`/docs` when running. This is a hand-written summary of the stable surface
(spec §57).

Every endpoint below except health/ready checks and the Slack/Teams webhook
signature/token verification requires a signed service token in the
`X-Service-Token` header (`common/service_auth.py`), scoped to the
receiving service's audience (`inumi-gateway`, `inumi-execution`,
`inumi-agent`).

## Gateway (`gateway/api/app.py`) — default port 8001

### `POST /v1/tool-calls`

Body: `ToolCallRequest` (`common/models/tool.py`) — `tool_id`, `arguments`,
`target`, `reason`, `conversation_id`, `investigation_id?`, `request_id`,
`channel`, `channel_account_id`, `approval_id?`, `change_id?`.

Response: `ToolCallResponse` — `status` (`EXECUTED`/`APPROVAL_REQUIRED`/
`DENIED`/`FAILED`), `execution_id?`, `approval_id?`, `result?`,
`failure_code?`, `message`, `risk?`, `policy_decision?`.

### `GET /v1/tools`, `GET /v1/tools/{tool_id}`

Lists the tool catalog. `GET /v1/tools?channel=...&channel_account_id=...`
filters to what that resolved role could attempt (convenience only — see
[SECURITY.md](SECURITY.md)).

### `POST /v1/approvals/{approval_id}/approve`, `.../reject`

Body: `{channel, channel_account_id}` — the *approver's* channel account,
independently re-resolved exactly like a tool call. Response includes the
resulting `status` (`APPROVED`, `AWAITING_SECOND_APPROVAL`, `REJECTED`).

### `GET /v1/approvals/{approval_id}`

Returns the approval's current state (never the raw action hash).

### `GET /v1/investigations/{investigation_id}`, `GET /v1/audit/{audit_event_id}`

Read-only projections of control-plane state.

### `GET /v1/catalog/servers`, `GET /v1/catalog/servers/{server_id}`

The registered servers (`config/servers.yaml`) plus, per server, the latest
discovered catalog — engine version/edition, database list, and per-database
object counts (tables, views, indexes, procedures) and available extensions.
Catalog discovery reads catalog and statistics views only; it never reads
table or view contents. `404` if the server id is not registered.

### `POST /v1/catalog/refresh`, `POST /v1/catalog/refresh/{server_id}`

Re-runs discovery now (otherwise it refreshes lazily on first use and every
`DISCOVERY_REFRESH_MINUTES`). Body: `{channel, channel_account_id}` —
independently re-resolved and required to hold `DBA_MANAGER` (`403`
otherwise). `404` if the server id is not registered.

### `GET /health`, `GET /ready`

## Execution Service (`execution/api/app.py`) — default port 8002

### `POST /v1/execute`

Only callable by the Gateway (audience `inumi-execution`). Body:
`ExecutionRequest`; response: `ExecutionResult`. Never exposed to any other
service or to the public internet in a real deployment (spec §30).

### `POST /v1/discover`

Only callable by the Gateway. Body: `DiscoveryRequest` (`server_id`,
`platform`, `max_objects_per_database`); response: `ServerCatalog`. Opens a
real connection via the same `CredentialProvider` as `/v1/execute` and reads
the engine's catalog/DMV/stats views — server properties, `sys.databases` /
`pg_database`, object lists with row estimates and sizes, available
extensions. It issues no query that returns user table/view data.

### `GET /health`, `GET /ready`

## Agent (`agent/api/app.py`) — default port 8000

### `POST /v1/chat`

Body: `{channel, channel_account_id, conversation_id, channel_thread_id?, message}`.
Response: `AgentReply` — `text`, `status` (`ok`/`approval_required`/`denied`/
`error`/`clarification`), `approval_card?`, `investigation_id?`.

### `POST /v1/chat/events`

Body: `{channel, channel_account_id, conversation_id, approval_id, decision}`
where `decision` is `"approve"` or `"reject"` — routes to the Gateway's
approval endpoints and resubmits the original tool call on success.

### `GET /health`

## Channels (`channels/api/app.py`) — default port 8003

### `POST /webhooks/slack`

Verifies `X-Slack-Signature`/`X-Slack-Request-Timestamp`
(`channels/slack/signature.py`), handles Slack's `url_verification`
challenge, and forwards `message` events from verified DBA accounts to the
Agent.

### `POST /webhooks/slack/interactive`

Handles Block Kit button clicks (Approve/Reject), routing to the Agent's
`/v1/chat/events`.

### `POST /webhooks/teams`

Verifies the Bot Framework bearer token (`channels/teams/auth.py`) and
routes both plain messages and Adaptive Card `Action.Submit` payloads
(approve/reject).

### `POST /dev/chat`, `POST /dev/chat/events` (spec §66)

Mock channel for local development — no real Slack/Teams credentials
required. Body: `{user, message, conversation_id?}` where `user` is a
`channel_accounts.dev` value from `config/identity.yaml` (e.g.
`"dba_l2@example.com"`). Still goes through real identity resolution and
the real Agent/Gateway/Execution pipeline — only the transport is mocked.

### `GET /health`
