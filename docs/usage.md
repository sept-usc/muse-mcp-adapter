# Usage Guide

This guide covers day-to-day use of the adapter: the administrator CLI, the policy file, running the service, the API Muse calls, and operations.

## 1. Administrator CLI

All commands take `--database <path>` (default `adapter.db`).

| Command | Purpose |
| --- | --- |
| `server-register` | Register an MCP server by exact endpoint. Endpoints must be HTTPS and public; a provider pack can be named with `--provider-pack`. |
| `server-list` | List registered servers. |
| `discover-remote` | Connect to the server and record its primitives. Use `--token-env NAME` to read a bearer token from an environment variable. |
| `discover` | Record primitives from a JSON manifest file (deterministic, for testing). |
| `publish` | Compile an immutable catalog version from the latest discovery and a policy file. |
| `activate` / `rollback` | Make a catalog version live, or return to the previous one. |
| `drift` | Compare the live server with the active catalog. |
| `status` | Show the server, latest discovery, and active catalog. |
| `server-enable` / `server-disable` | Stop or resume all runtime traffic for a server. |
| `serve` | Run the Muse-facing HTTP service. |

Every administrative action is recorded with the acting user in the audit trail.

## 2. Policy file

A policy file is a JSON array with one entry per primitive you want to expose. Anything not listed is not exposed. A discovered tool is identified as `tool:<name>`, a resource as `resource:<name>`, a template as `resource_template:<name>`, and a prompt as `prompt:<name>`.

Common fields:

| Field | Meaning |
| --- | --- |
| `primitiveKey`, `allowed`, `publicOperationId` | Which primitive, whether exposed, and the stable operation name Muse sees (`[a-z][a-z0-9_]{2,63}`). |
| `classification` | `read`, `write`, or `sensitive_write`. Resources, templates, and prompts must be `read`. |
| `requiredScopes` | Downstream scopes the credential must hold (default none). |
| `timeoutSeconds` | Tool call timeout, 1 to 300 (default 30). |
| `maxResultBytes` | Tools: maximum result size (required, up to 16 MiB). |
| `readOnlyReviewed` | Set to `true` to classify a tool as `read` when the server did not mark it `readOnlyHint: true`. |
| `allowedResultContentTypes` | Optionally narrow a tool's accepted result content types. |

Resource and template policies add a `resource` block; prompts add a `prompt` block:

```json
{
  "primitiveKey": "resource_template:issue_document",
  "allowed": true,
  "publicOperationId": "read_issue_document",
  "classification": "read",
  "resource": {
    "allowedMimeTypes": ["text/plain", "text/markdown"],
    "maxBytes": 262144,
    "allowedUriSchemes": ["mcp"],
    "argumentPatterns": {"issueId": "[A-Z]+-[0-9]+"}
  }
}
```

```json
{
  "primitiveKey": "prompt:triage_issue",
  "allowed": true,
  "publicOperationId": "get_triage_prompt",
  "classification": "read",
  "prompt": {
    "allowedRoles": ["user", "assistant"],
    "allowedEmbeddedMimeTypes": ["text/plain"],
    "maxBytes": 262144
  }
}
```

Rules enforced when you publish:

- A tool without `readOnlyHint: true` cannot be classified `read` unless `readOnlyReviewed` is set.
- `file:`, `data:`, and similar schemes are never allowed for resources. `http(s)` resources must list `allowedUriHosts`.
- Template arguments can be constrained with `argumentPatterns` (regular expressions that must match the whole value).
- Oversized or backtracking-prone regular expressions in schemas and patterns are rejected.

## 3. Running the service

`serve` reads a configuration file that names environment variables. Secret values are never stored in the file.

```json
{
  "artifactRoot": "artifacts",
  "approvalSecretEnv": "MUSE_APPROVAL_SECRET",
  "integrityKeyEnv": "MUSE_CATALOG_INTEGRITY_KEY",
  "listen": {"host": "127.0.0.1", "port": 8080},
  "limits": {"rateLimit": 60, "rateWindowSeconds": 60, "perServerConcurrency": 8},
  "ingress": [
    {"tokenEnv": "MUSE_TOKEN_A", "identity": "user-1",
     "servers": ["project-system"], "connections": ["connection-1"]}
  ],
  "credentials": [
    {"identity": "user-1", "connection": "connection-1", "server": "project-system",
     "ref": "secret://project-system/user-1", "tokenEnv": "DOWNSTREAM_TOKEN_A",
     "scopes": ["issues:read", "issues:write"]}
  ]
}
```

| Setting | Purpose |
| --- | --- |
| `ingress` | Who may call the adapter. Each entry maps a bearer token (read from `tokenEnv`) to an identity and the servers and connections it may use. Add `"administrator": true` for metrics and cross-user access. |
| `credentials` | Which downstream credential belongs to which identity, connection, and server. A credential reference cannot be shared between bindings. |
| `approvalSecretEnv` | Shared secret used to verify signed write approvals from the Muse-side approval authority (at least 32 bytes). |
| `integrityKeyEnv` | Optional key that signs stored catalogs so tampering is detected. |
| `allowPrivateNetworks` | Set `true` only if your MCP servers are on private addresses. |

The adapter authenticates to MCP servers with the bearer or service tokens named in `credentials`. Run the service behind TLS (for example a reverse proxy); it listens on plain HTTP.

## 4. The API Muse calls

| Route | Purpose |
| --- | --- |
| `GET /openapi.json?serverId=...` | The Muse-facing contract compiled from the active catalog. |
| `POST /v1/operations/{publicOperationId}` | Run a published tool, resource read, or prompt. |
| `GET /v1/executions/{operationId}` | Operation state (owner or administrator only). |
| `POST /v1/executions/{operationId}/resume` | Supply input an operation asked for. |
| `POST /v1/executions/{operationId}/cancel` | Cancel (see below). |
| `GET /v1/artifacts/{artifactId}` | Fetch a stored artifact belonging to the caller's operation. |
| `GET /metrics`, `GET /health` | Counters (administrator only) and liveness. |

A request body names the server and connection and carries the arguments:

```json
{
  "serverId": "project-system",
  "connectionId": "connection-1",
  "arguments": {"title": "Login fails on Safari"},
  "idempotencyKey": "create-2026-10-05-001",
  "approval": {"approvalId": "...", "classification": "write", "actionDigest": "...",
               "approvedAt": "...", "expiresAt": "...", "signature": "..."}
}
```

The caller's identity comes from the bearer token and cannot be set in the body.

**Writes** require an `idempotencyKey` and approval. If `approval` is missing or invalid, the response is `409` with an `actionDigest`. The approval authority signs evidence for that exact action and the caller retries. Repeating the same request never runs the write twice.

**Statuses:** `200` success, `202` cancellation requested, `400` invalid request, `401` not authenticated, `403` not authorized or server disabled, `404` unknown operation, `409` approval, input, idempotency, or unknown-outcome conflict, `413` body too large, `429` rate limited, `502` downstream failure.

**Cancellation:** work that has not started is cancelled immediately (`200`, `"cancellation": "applied"`). A write that is already running cannot be aborted safely, so the request is recorded and the operation resolves to its real outcome (`202`, `"cancellation": "requested"`). A dispatched write is never reported as cancelled.

## 5. Operations

- **Drift:** run `drift` regularly. Added primitives are reported for review; removed primitives and changed schemas, annotations, or scopes are marked as blocking. Nothing changes the live catalog until you publish and activate a new version.
- **Rollback:** `rollback` returns to the previously active version.
- **Emergency stop:** `server-disable` blocks all runtime traffic for a server immediately.
- **Unknown write outcomes:** if the adapter cannot tell whether a write happened, the operation becomes `UNKNOWN` and further retries are blocked until an operator confirms the real outcome (not applied, or completed with its result). Reconciliation records who did it.
- **Restart recovery:** operations left running by a crash are recovered at start-up: reads fail as retryable, writes become `UNKNOWN`.
- **Audit:** administrative actions and every runtime request are recorded in the database; operation events (state changes, progress, input, reconciliation) are kept per operation. Token-like values are redacted.
