# Muse MCP Adapter

A customer-owned, single-customer gateway that presents a stable Muse Connector surface while acting as an MCP client to provider-operated or customer-operated MCP servers.

## Why this project exists

Muse supports approved platform Connectors and user-created Custom Connectors, but it does not currently document a native MCP-client setup flow. MCP servers, meanwhile, expose a protocol-driven and potentially dynamic surface: tools, resources, prompts, notifications, elicitation, authorization, and long-running tasks.

This project bridges those models without silently weakening Muse permissions or changing an approved connector when an upstream MCP server changes.

```text
Muse
  -> stable Muse-facing connector API
  -> policy and schema boundary
  -> MCP client and authorization broker
  -> customer MCP server
```

## Why use the adapter

Directly teaching Muse about one MCP server can be sufficient for a small proof of concept. The adapter becomes valuable when one customer needs a reusable integration across multiple MCP servers:

- It implements MCP once, including tools, resources, prompts, authentication, and lifecycle behavior, instead of rebuilding those details in every Custom Connector.
- It gives Muse a stable, versioned contract while upstream MCP capabilities and schemas evolve.
- It applies explicit Muse Read, Write, and Sensitive Write policy rather than trusting MCP annotations as authorization decisions.
- It isolates local users, service identities, downstream accounts, credentials, operations, and MCP connections inside the customer's deployment.
- It provides versioned provider packs and conformance tests that the customer can use as working examples for additional MCP servers.

The tradeoff is an additional service to deploy, secure, monitor, and operate. For one simple, stable MCP server, direct onboarding may remain the better choice.

## Current status

The repository uses Python 3.12+. The initial seven implementation phases are complete. The contract foundation and durable SQLite control plane feed read and tool data planes that execute only active published operations. The official MCP Python SDK provides the remote protocol boundary. Tool writes require signed, action-bound approval evidence and idempotency, persist guarded operation states, cache completed results, and block uncertain retries until explicit, actor-attributed reconciliation.

The initial scope is:

- Remote MCP servers over Streamable HTTP.
- MCP tool discovery and invocation.
- MCP resource discovery, template discovery, and reads.
- MCP prompt discovery and argument-based retrieval.
- A versioned, allowlisted Muse-facing primitive catalog.
- Explicit Muse Read, Write, and Sensitive Write policy.
- OAuth or bearer-token authentication to downstream MCP servers.
- Schema-drift detection and safe error translation.
- Text and structured JSON results.
- Durable progress, input-required/resume, cancellation, artifacts, audit events, and restart recovery.
- Customer-operated rate limits, concurrency limits, metrics, and outbound endpoint policy.

Local `stdio` servers, MCP Apps, a hosted provider marketplace, billing, and multi-customer tenancy are out of scope. The adapter is deployed and owned by one customer. Tools, resources, and prompts are all included in this first draft.

## Implementation phases

Each phase has a runnable test gate and leaves the repository in a usable state without depending on unfinished later phases.

| Phase | Status | Functionality | Independent test gate |
| --- | --- | --- | --- |
| 1 — Contract foundation | Complete | Canonical primitive model, deterministic digests, policy validation, immutable catalog compilation, and drift classification | Unit tests for normalization, policy, compilation, and drift; end-to-end discovery-to-publication test covering tools, resources, templates, and prompts |
| 2 — Control plane | Complete | Server registration, discovery boundary, catalog persistence, activation, rollback, drift reporting, and administrative CLI | Repository and service unit tests; end-to-end CLI test against a deterministic MCP discovery fixture with no data-plane dependency |
| 3 — Read data plane | Complete | Muse ingress boundary, published-catalog routing, credential references, resource reads, prompt retrieval, content validation, and result normalization | Executor unit tests; end-to-end resource and prompt flows through a fake Muse client and conformance MCP server |
| 4 — Tool execution safety | Complete | Tool calls, input/output validation, approval evidence, idempotency, durable operation states, typed error mapping, and uncertain-write recovery | State-machine and retry unit tests; end-to-end read/write/unknown-outcome/duplicate-prevention scenarios, including restart durability |
| 5 — Remote MCP and OAuth | Complete | Official MCP SDK, stateless Streamable HTTP, protocol allowlisting, bearer authentication, OAuth/PKCE and scope challenges, connection-scoped token storage, progress, timeout, and cancellation behavior | SDK contract tests plus an end-to-end Streamable HTTP MCP server covering tool, resource, and prompt discovery |
| 6 — Provider packs | Complete | Validated generic pack format, shared conformance runner, and Linear, Atlassian, and Supabase reference packs | Pack schema, endpoint, safe-default, and shared-runner tests proving there is no provider-specific execution fork |
| 7 — Lifecycle and production hardening | Complete | Input-required/resume and cancellation states, durable progress/audit events, bounded artifact storage, metrics, rate/concurrency controls, endpoint policy, and safe restart recovery | Restart, artifact-integrity, redaction, rate, concurrency, endpoint-security, and lifecycle end-to-end tests |

## Reference provider packs

Provider packs supply endpoint and safety configuration to the same generic runtime; they do not contain custom provider execution code or automatically authorize discovered tools.

| Pack | Official endpoint | Safe starting point |
| --- | --- | --- |
| [Linear](https://linear.app/docs/mcp) | `https://mcp.linear.app/mcp` | Prefer the documented read-only endpoint while validating the catalog; explicitly classify and approve writes |
| [Atlassian](https://atlassian.github.io/atlassian-mcp-server/) | `https://mcp.atlassian.com/v2/mcp` | Use least-privilege OAuth scopes or API-token permissions; explicitly classify and approve writes |
| [Supabase](https://supabase.com/docs/guides/ai-tools/mcp) | `https://mcp.supabase.com/mcp` | Require `project_ref`, default `read_only=true`, and restrict enabled feature groups |

These packs are reference onboarding configurations, not a claim that every live provider capability is safe to publish. Each deployment must run discovery, review drift, define its own exposure policy, and validate against the provider account it controls.

## Development and tests

Create and populate the local environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Run the independently separated test suites and quality checks:

```bash
.venv/bin/pytest tests/unit
.venv/bin/pytest tests/e2e
.venv/bin/pytest
.venv/bin/ruff check --no-cache src tests
.venv/bin/pyright
```

## Phase 2 administrative CLI

The control plane is independently usable with a local SQLite database:

```bash
.venv/bin/muse-mcp-adapter --database adapter.db server-register \
  --server-id project-system \
  --display-name "Project System" \
  --endpoint https://mcp.example.test/mcp \
  --auth-profile delegated_oauth

.venv/bin/muse-mcp-adapter --database adapter.db discover \
  --server-id project-system \
  --manifest discovery-manifest.json

.venv/bin/muse-mcp-adapter --database adapter.db publish \
  --server-id project-system \
  --catalog-version 1.0.0 \
  --policies policies.json

.venv/bin/muse-mcp-adapter --database adapter.db activate \
  --server-id project-system \
  --catalog-version 1.0.0

.venv/bin/muse-mcp-adapter --database adapter.db drift --server-id project-system
.venv/bin/muse-mcp-adapter --database adapter.db status --server-id project-system
.venv/bin/muse-mcp-adapter --database adapter.db rollback --server-id project-system
```

The manifest client remains available for deterministic control-plane tests. Production integrations use the Phase 5 SDK client; both implementations satisfy the same discovery boundary.

## Running the Muse-facing ingress

`serve` starts the authenticated HTTP ingress. The configuration file names environment variables only; token, approval-secret, and credential values are never stored in it.

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

```bash
.venv/bin/muse-mcp-adapter --database adapter.db serve --config serve.json
```

| Route | Purpose |
| --- | --- |
| `POST /v1/operations/{publicOperationId}` | Execute a published tool, resource, or prompt |
| `GET /v1/executions/{operationId}` | Operation state (owner or administrator only) |
| `POST /v1/executions/{operationId}/resume` and `/cancel` | Supply requested input / cancel (`200` applied, `202` requested for a running write) |
| `GET /openapi.json?serverId=...` | Muse contract compiled from the active catalog |
| `GET /metrics`, `GET /v1/artifacts/{id}` | Administrator metrics; operation-owned artifacts |

The caller's identity, servers, and connections come from the authenticated bearer token, never from the request body. Errors map to actionable statuses: 401/403 authentication and authorization, 400 validation, 404 unknown operation, 409 approval/input/idempotency/unknown-outcome, 413 oversized body, 429 rate limit, 502 downstream failure.

Known limitations: `serve` authenticates to downstream servers with bearer/service tokens from the environment. Interactive delegated OAuth needs a deployment-supplied redirect/callback handler and is not wired into `serve`. Provider packs describe endpoints, options, and safety rules but do not yet ship default allowlists or recorded discovery snapshots.

## Security controls

- **Approvals** are HMAC-signed by the Muse-side authority, bound to the exact action digest and classification, time-limited (15 minutes for writes, 5 for sensitive writes), and single-use per operation.
- **Endpoints** must be HTTPS, userinfo-free, and public. Hostnames are resolved and every address checked, numeric forms such as `2130706433` or `0x7f.1` are rejected, and the connection is pinned to the checked addresses. Redirects are not followed, and responses are size- and time-limited.
- **Policy** defaults are explicit (`requiredScopes: []`, `timeoutSeconds: 30`) and payload limits are bounded. Tools without `readOnlyHint: true` cannot be classified `read` unless `readOnlyReviewed` is set. Resource URIs cannot use `file:`/`data:`-style schemes, HTTP(S) resources need `allowedUriHosts`, and template arguments can be constrained with `argumentPatterns`. Oversized or backtracking-prone schema regexes are rejected at publish.
- **Operations never strand in `RUNNING`**: failures before dispatch are retryable, failures after dispatch of a write become `UNKNOWN`, and stale `RUNNING` operations are recovered at start-up. A write whose result is rejected is `UNKNOWN`, not `FAILED`.
- **Storage**: the database and artifacts are owner-only, catalogs can be HMAC-signed (`integrityKeyEnv`), OAuth state can be encrypted at rest, and audit output redacts token-like keys and bearer values.
- **Audit**: administrative actions record the actor (`server.register`, `server.discover`, `catalog.publish`, `catalog.activate`, `catalog.rollback`, `server.enable|disable`), and every ingress request writes a runtime audit row.

## Documentation

- [System design](docs/design.md)
- [Control plane](docs/control-plane.md)
- [Data plane](docs/data-plane.md)

## Repository principles

1. Treat downstream MCP metadata as untrusted input.
2. Never infer a Muse-sensitive operation solely from MCP annotations.
3. Keep the Muse-facing contract stable and versioned.
4. Detect upstream schema drift before it affects users.
5. Preserve user, connection, credential, and authorization boundaries end to end.
6. Prefer explicit state and idempotency over implicit retries.

## Next validation milestone

The repository is now an independently tested adapter core. The next milestone is deployment integration: expose the customer-specific Muse Custom Connector ingress, connect the customer's secret store and approval authority, configure production observability, and run credentialed conformance tests against the customer's selected Linear, Atlassian, and Supabase accounts. Live provider tests are intentionally not part of the credential-free local suite.
