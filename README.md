# Muse MCP Adapter

A self-hosted gateway that lets **Muse Custom Connectors use MCP servers safely**. It sits between Muse and the MCP servers your organization relies on, exposes only the capabilities you approve, and enforces who can do what.

## What is it?

[MCP](https://modelcontextprotocol.io) (Model Context Protocol) servers expose tools, data, and prompt templates from applications such as issue trackers, wikis, and databases. Muse integrates services through Connectors and Custom Connectors with defined operations and Read / Write / Sensitive Write permissions. It does not document a native way to connect to an MCP server.

The adapter bridges the two. You run it inside your own environment. It speaks MCP to your servers and presents Muse with a stable, versioned, reviewed API.

```text
Muse Custom Connector
        │  stable, authenticated HTTPS API
        ▼
 Muse MCP Adapter  ── your policy · approvals · limits · audit
        │  MCP (Streamable HTTP)
        ▼
 Your MCP servers  (Linear, Atlassian, Supabase, internal tools, ...)
```

## Why do you need it?

MCP servers change on their own: tools appear, schemas shift, and a tool marked "read-only" may not be. Connecting Muse to that directly would mean either rebuilding MCP support in every Custom Connector or letting upstream changes alter what Muse can do without anyone noticing.

The adapter solves this once, for every server:

- **You decide what Muse can reach.** Nothing is exposed until an administrator reviews it and publishes a versioned catalog. New or changed upstream tools never go live automatically.
- **Permissions are yours, not the server's.** Each operation is classified Read, Write, or Sensitive Write by your policy. MCP annotations are treated as hints, never as authorization.
- **Writes are guarded.** Writes need a signed approval for that exact action and an idempotency key, and an unclear outcome is never blindly retried.
- **Upstream drift is caught.** The adapter compares live servers with the published catalog and reports added, removed, or changed capabilities.
- **Users and credentials stay separated.** Every request is tied to an authenticated caller, a connection, and a credential that cannot be used for anyone else.
- **It is operable.** Rate and concurrency limits, audit trails, metrics, and restart recovery are built in.

The trade-off is one more service to run and secure. For a single simple, stable MCP server, onboarding it directly may be enough. The adapter pays off when you want several servers behind one reviewed, consistent boundary.

## How it works

| Step | Who | What happens |
| --- | --- | --- |
| 1. Register | Administrator | Add an MCP server by its exact endpoint (optionally using a provider pack). |
| 2. Discover | Administrator | The adapter connects and records what the server offers: tools, resources, resource templates, prompts. |
| 3. Review and publish | Administrator | You write a policy choosing which primitives to expose, how each is classified, and its limits. The adapter compiles an immutable, versioned catalog. |
| 4. Activate | Administrator | Make a catalog version live, or roll back to the previous one. |
| 5. Serve | Operator | Run the HTTP service. Muse calls only operations in the active catalog. |
| 6. Monitor | Administrator | Check drift against the live server, review audit logs, and republish when a change is acceptable. |

At runtime every request is authenticated, checked against the active catalog and your policy, validated against the published schema, executed against the MCP server with the right credential, validated again on the way back, and recorded in the audit log.

## Quick start

Requires Python 3.12+.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Register a server, discover it, publish a reviewed policy, and activate it:

```bash
adapter=".venv/bin/muse-mcp-adapter --database adapter.db"

$adapter server-register --server-id project-system --display-name "Project System" \
  --endpoint https://mcp.example.com/mcp --auth-profile bearer_token

$adapter discover-remote --server-id project-system --token-env PROJECT_SYSTEM_TOKEN
$adapter publish  --server-id project-system --catalog-version 1.0.0 --policies policies.json
$adapter activate --server-id project-system --catalog-version 1.0.0
```

A policy states exactly what is exposed. This one allows one read tool and one write tool:

```json
[
  {
    "primitiveKey": "tool:find_issue",
    "allowed": true,
    "publicOperationId": "find_issue",
    "classification": "read",
    "requiredScopes": ["issues:read"],
    "maxResultBytes": 262144
  },
  {
    "primitiveKey": "tool:create_issue",
    "allowed": true,
    "publicOperationId": "create_issue",
    "classification": "write",
    "requiredScopes": ["issues:write"],
    "maxResultBytes": 262144
  }
]
```

Start the service and point Muse at it:

```bash
$adapter serve --config serve.json
```

Muse then uses the contract served at `/openapi.json?serverId=project-system`, and calls operations at `/v1/operations/{publicOperationId}` with a bearer token that identifies the caller. Everything else, including the full policy reference, the `serve` configuration, and the API, is in the [usage guide](docs/usage.md).

Day to day, an administrator checks for upstream changes with `drift`, inspects state with `status`, and reverts a bad release with `rollback`.

## Reference provider packs

Provider packs hold endpoint and safety settings for common servers. They add no provider-specific code and never authorize discovered tools automatically.

| Pack | Endpoint | Safe starting point |
| --- | --- | --- |
| [Linear](https://linear.app/docs/mcp) | `https://mcp.linear.app/mcp` | Start from the documented read-only endpoint; classify and approve writes explicitly |
| [Atlassian](https://atlassian.github.io/atlassian-mcp-server/) | `https://mcp.atlassian.com/v2/mcp` | Use least-privilege scopes; classify and approve writes explicitly |
| [Supabase](https://supabase.com/docs/guides/ai-tools/mcp) | `https://mcp.supabase.com/mcp` | Require `project_ref`; `read_only=true` by default |

These are starting points, not a claim that everything a provider offers is safe to publish. Run discovery, review it, and write your own policy for the account you control.

## Scope

**Supported:** remote MCP servers over Streamable HTTP; tools, resources, resource templates, and prompts; authentication to MCP servers with bearer or service tokens; text and structured JSON results.

**Not included:** local `stdio` servers, MCP Apps rendering, a hosted marketplace, billing, or multi-customer tenancy. Each deployment belongs to one customer.

## Documentation

- [Usage guide](docs/usage.md): configuration, policy reference, API, and operations
- [Security model](docs/security.md): controls and guarantees
- [System design](docs/design.md)
- [Control plane](docs/control-plane.md) and [data plane](docs/data-plane.md)

## Development

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check --no-cache src tests
.venv/bin/pyright
```
