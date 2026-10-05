# Security Model

The adapter treats everything an MCP server sends as untrusted, and everything exposed to Muse as an explicit decision made by the customer.

## Trust boundaries

```text
Muse caller ──authenticated──▶ Adapter ──policy-checked──▶ MCP server ──▶ application
```

1. The caller authenticates to the adapter with a bearer token. Identity, allowed servers, and allowed connections come from that token, never from the request body.
2. The adapter authenticates to the MCP server with a credential bound to exactly one identity, connection, and server.
3. Muse never receives MCP endpoints, tokens, or raw tool access. It can only call operations in the active published catalog.

## Controls

**Exposure**
- Nothing is exposed until an administrator publishes a policy. Discovery and drift never change the live catalog.
- Classification (Read, Write, Sensitive Write) comes from customer policy. MCP annotations are evidence only; a tool without `readOnlyHint: true` cannot be classified `read` without an explicit review flag.
- Requests are validated against the published schema, not a fresh discovery, and results are validated before they are returned.

**Write safety**
- Writes need an idempotency key and approval evidence that is HMAC-signed by the Muse-side authority, bound to the exact action and classification, short-lived (15 minutes for writes, 5 for sensitive writes), and usable for one operation only.
- Repeating a request returns the stored result rather than running the write again.
- If the outcome cannot be established (a failure after the request was sent, a rejected result, or a crash), the write is marked `UNKNOWN` and is not retried until an operator reconciles it. Failures that happen before anything was sent are safely retryable.
- An operation never remains in `RUNNING` after an error.

**Outbound connections**
- Endpoints must use HTTPS, contain no embedded credentials, and resolve only to public addresses. Numeric host tricks (for example `2130706433` or `0x7f.1`) and DNS names that resolve to private addresses are rejected, and the connection is pinned to the addresses that were checked.
- Redirects are not followed. Responses are limited in size and every request has a time limit.
- Resource URIs cannot use schemes such as `file:` or `data:`. HTTP resources must name the hosts they may reach, and template arguments are encoded and can be pattern-restricted.

**Isolation**
- Operations, idempotency keys, tokens, and artifacts are scoped by identity, connection, and server. A caller can read only its own operations (administrators excepted).
- A credential reference cannot be bound to more than one identity, connection, or server. OAuth tokens that do not cover the required scopes are not used.

**Availability**
- Per-caller rate limits, per-server concurrency limits, request size limits, bounded progress events, and bounded artifacts.
- A server can be disabled instantly with `server-disable`.

**Data at rest and audit**
- The database and artifacts are created owner-only. Catalogs can be HMAC-signed so tampering is detected. OAuth state can be encrypted at rest.
- Administrative actions (who registered, discovered, published, activated, rolled back, or disabled) and every runtime request are recorded. Logs and audit data redact token-like keys and bearer values.

## Your responsibilities

- Provide secrets (ingress tokens, downstream tokens, approval secret, optional integrity key) through environment variables or your secret manager, and rotate them.
- Run the service behind TLS and restrict network access to it.
- Review discovery output and drift before publishing, and keep policies least-privilege.
- Keep the Muse-side approval authority's signing secret private.
