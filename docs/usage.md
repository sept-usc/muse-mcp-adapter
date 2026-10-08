# Usage Guide

This guide covers day-to-day use of the adapter: the command-line tool, the policy file, running the service, connecting Muse, the API, and operations.

## 1. Command-line tool

Global options go before the command: `--database <path>` (default `adapter.db`), `--config <serve.json>` (or set `MUSE_ADAPTER_CONFIG`), and `--actor <name>` (recorded in the audit trail; defaults to `$USER`).

| Command | Purpose |
| --- | --- |
| `server-register` | Register an MCP server by exact endpoint. Endpoints must be HTTPS and public; name a provider pack with `--provider-pack`. |
| `server-list` | List registered servers. |
| `discover-remote` | Connect to the server and record its primitives. Use `--token-env NAME` for a static token, or `--connection-id` to use a connection's credential (for OAuth servers, sign in first). |
| `discover` | Record primitives from a JSON manifest file (deterministic, for testing). |
| `policy-template` | Draft a policy from the latest discovery. Everything starts disallowed; `--expose-reads` allows read-only primitives. Writes always start disallowed. |
| `publish` | Compile an immutable catalog version from the latest discovery and a policy file. |
| `activate` / `rollback` | Make a catalog version live, or return to the previous one. |
| `drift` | Compare the live server with the active catalog. |
| `status` | Show a server, its latest discovery, its active catalog, and its connections. |
| `link` | Create a connection for Muse: connector URL, bearer token, connection code (see section 4). |
| `links` / `unlink` | List connections / revoke one (also ends its OAuth sessions). |
| `rotate-token` | Issue a new token and connection code, ending every session of a connection. |
| `link-code` | Issue a fresh connection code (the old one stops working). |
| `login` | Print a one-time sign-in link for a connection that uses OAuth to its MCP server. |
| `reconcile` | Settle a write whose outcome is unknown after you have checked the downstream system. |
| `server-enable` / `server-disable` | Resume or stop all traffic for a server. |
| `serve` | Run the Muse-facing HTTP service. |

For local testing against a plain-HTTP or loopback server, add `--allow-http` and/or `--allow-private-network` before the command. They are off by default and meant for development only.

## 2. Policy file

A policy file is a JSON array with one entry per primitive you want to expose. Anything not listed, or with `"allowed": false`, is not exposed. A discovered tool is identified as `tool:<name>`, a resource as `resource:<name>`, a template as `resource_template:<name>`, and a prompt as `prompt:<name>`. Start from `policy-template`.

Common fields:

| Field | Meaning |
| --- | --- |
| `primitiveKey`, `allowed`, `publicOperationId` | Which primitive, whether exposed, and the stable operation name Muse sees (`[a-z][a-z0-9_]{2,63}`; `get_operation_status` and `resume_operation` are reserved). |
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

`serve` reads a configuration file. It holds no secrets: it names environment variables.

```json
{
  "publicUrl": "https://adapter.example.com",
  "listen": {"host": "0.0.0.0", "port": 8080},
  "limits": {"rateLimit": 60, "rateWindowSeconds": 60, "perServerConcurrency": 8},
  "integrityKeyEnv": "MUSE_CATALOG_INTEGRITY_KEY"
}
```

| Setting | Purpose |
| --- | --- |
| `publicUrl` | The HTTPS address Muse reaches the service at. It appears in the connector URL and the sign-in pages. Required unless listening on a local address. |
| `listen` | Host and port to bind (default `127.0.0.1:8080`). |
| `limits` | Per-connection request rate and per-server concurrency. |
| `approvalMode` | `muse` (default): Muse confirms writes with you. `signed`: additionally require signed approval evidence (needs `approvalSecretEnv`). |
| `integrityKeyEnv` | Optional key that signs stored catalogs so tampering is detected. Use the same config for every command that publishes. |
| `secretsDir` | Optional directory of secret files, one file per name (for example a Docker or Compose secrets mount). A secret that is not in the environment is read from `<secretsDir>/<NAME>` when it is needed, so it stays out of the process environment and can be added without a restart. `MUSE_ADAPTER_SECRETS_DIR` sets the same thing. |
| `oauthClients` | For an OAuth provider without dynamic client registration: `{"<serverId>": {"clientId": "...", "clientSecretEnv": "..."}}`. |
| `allowPrivateNetworks`, `allowInsecureHttp` | Development only: allow loopback/private MCP servers and plain HTTP. |

Next to the database, `serve` keeps an owner-only key file and an encrypted store for OAuth tokens. Run the service behind TLS (for example a reverse proxy or a tunnel); it listens on plain HTTP itself.

## 4. Connecting Muse

`link` creates one connection:

```bash
muse-mcp-adapter --database adapter.db --config serve.json link \
  --server-id project-system --label "My Muse" --credential env_token --token-env PROJECT_SYSTEM_TOKEN
```

| `--credential` | How the adapter authenticates to the MCP server |
| --- | --- |
| `none` | No authentication. |
| `env_token` | The token in the environment variable named by `--token-env`, read when needed and never stored. |
| `oauth` | Your own account at the provider. `link` prints a one-time sign-in link; open it in your browser. Tokens are stored encrypted and refreshed automatically. If they ever stop working, the adapter tells Muse to reconnect and includes a fresh sign-in link. |

The command prints:

- the **connector URL** (an unguessable public address of the API description),
- a **bearer token**,
- a **connection code** (valid for 24 hours; issue a new one with `link-code`),
- the sentence to say to Muse.

In Muse's chat, ask it to create a connector from the connector URL. Muse reads the API description and offers a way to authenticate:

- **Sign in (OAuth).** Muse opens a page asking for the connection code. Enter it. If the connection also uses OAuth to the MCP server, you are first taken through the provider's own consent. Muse then holds short-lived tokens that it refreshes itself.
- **Bearer token.** Give Muse the token when it asks.

The connection code proves you are the owner. Anyone who finds the connector URL still cannot link a Muse to it without the code, and five wrong codes lock sign-in for 15 minutes. Use `rotate-token` if a token or code leaks: it ends every session for that connection.

## 5. The API Muse calls

Everything is under the connector URL's base, `https://<publicUrl>/c/<slug>`:

| Route | Purpose |
| --- | --- |
| `GET /openapi.json` | The API description Muse reads. Public, and contains no secrets. |
| `POST /operations/{operationId}` | Run a published tool, resource read, or prompt. The JSON body is that primitive's own arguments. |
| `GET /executions/{operationId}` | State of an earlier operation (only for the same connection). |
| `POST /executions/{operationId}/resume` | Answer a question an operation asked (`{"arguments": {...}, "inputs": {...}}`). |
| `GET /artifacts/{artifactId}` | Fetch a stored artifact from the connection's own operation. |

Sign-in endpoints (`/oauth/authorize`, `/oauth/token`, `/oauth/register`, `/oauth/revoke`) and the standard well-known discovery documents are published for the connector too. A 401 response includes a `WWW-Authenticate` header pointing clients to them.

The caller is identified by the bearer token: it fixes the owner, the connection, and the server. Nothing about those is sent in a request.

**Writes** are confirmed by Muse with you before it calls them. Send an optional `Idempotency-Key` header when retrying. If you do not, identical writes sent within five minutes are treated as one, so a retried write runs once.

**Statuses:** `200` success, `400` invalid request, `401` not authenticated (or the linked account must be reconnected: the body then carries an `authorizationUrl`), `403` not allowed or connector disabled, `404` unknown, `409` input needed, still running, conflicting key, or unknown outcome, `413` body too large, `429` rate limited, `502` downstream failure.

## 6. Operations

- **Drift:** run `drift` regularly. Added primitives are reported for review; removed primitives and changed schemas, annotations, or scopes are marked as blocking. Nothing changes the live catalog until you publish and activate a new version.
- **Rollback:** `rollback` returns to the previously active version.
- **Emergency stop:** `server-disable` blocks all runtime traffic for a server; `unlink` stops one connection.
- **Unknown write outcomes:** if the adapter cannot tell whether a write happened, the operation becomes `UNKNOWN` and further retries are blocked until you check the downstream system and run `reconcile` (`--outcome not_applied`, or `completed` with a `--response` file). Reconciliation records who did it.
- **Restart recovery:** operations left running by a crash are recovered at start-up: reads fail as retryable, writes become `UNKNOWN`.
- **Audit:** administrative actions and every runtime request are recorded in the database; operation events (state changes, progress, input, reconciliation) are kept per operation. Token-like values are redacted.

## Try it locally

You need no Muse account and no real provider to see the whole flow. The repository includes a small MCP server and a script that behaves like Muse.

```bash
# 1. a sample MCP server on http://127.0.0.1:9101/mcp
.venv/bin/python examples/sample_mcp_server.py &

# 2. configure the adapter for local use
cat > serve.json <<'JSON'
{"publicUrl": "http://127.0.0.1:8123", "allowInsecureHttp": true,
 "allowPrivateNetworks": true, "listen": {"host": "127.0.0.1", "port": 8123}}
JSON
adapter=".venv/bin/muse-mcp-adapter --database adapter.db --config serve.json --allow-http --allow-private-network"

# 3. register, discover, publish
$adapter server-register --server-id sample --display-name Sample \
  --endpoint http://127.0.0.1:9101/mcp --auth-profile none
$adapter discover-remote --server-id sample
$adapter policy-template --server-id sample --expose-reads > policies.json
#    edit policies.json and set "allowed": true for create_issue to try a write
$adapter publish  --server-id sample --catalog-version 1.0.0 --policies policies.json
$adapter activate --server-id sample --catalog-version 1.0.0

# 4. link, serve, and act as Muse
$adapter link --server-id sample --label "My Muse" --credential none > link.json
$adapter serve &
.venv/bin/python examples/simulate_muse.py link.json \
  read '{"issue_id": "ISSUE-1"}' find_issue \
  write '{"title": "From Muse"}' create_issue
```

The simulator reads the public spec, registers, signs in with the connection code, and calls the operations with the token it received, the same steps Muse takes.
