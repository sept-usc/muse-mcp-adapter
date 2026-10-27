# Security Model

The adapter treats everything an MCP server sends as untrusted, and everything exposed to Muse as an explicit decision made by the owner. Because Muse reaches it over the internet, the public surface is kept small and every credential is scoped to one connection.

## Trust boundaries

```text
Muse ──(token or OAuth)──▶ Adapter ──(policy-checked, your credential)──▶ MCP server ──▶ application
```

1. Muse authenticates to the adapter with a per-connection credential: either a bearer token, or a short-lived OAuth access token it obtained by signing in. That credential identifies the owner, the connection, and the server; nothing about those is trusted from a request body.
2. The adapter authenticates to the MCP server with a credential that belongs to exactly one connection: none, a token read from the environment, or the owner's own OAuth tokens.
3. Muse never receives MCP endpoints, provider tokens, or raw tool access. It can only call operations in the active published catalog.

## Controls

**Exposure**
- Nothing is exposed until you publish a policy. Discovery and drift never change the live catalog.
- Classification (Read, Write, Sensitive Write) comes from your policy. MCP annotations are evidence only; a tool without `readOnlyHint: true` cannot be classified `read` without an explicit review flag.
- Requests are validated against the published schema, not a fresh discovery, and results are validated before they are returned.
- The public API description names your tools and their input schemas but holds no secrets. Its address is unguessable and can be revoked by unlinking the connection or disabling the server.

**Linking Muse (the adapter's OAuth server)**
- Sign-in uses the authorization code flow with PKCE (S256 only). Authorization codes live one minute, work once, and are bound to the client, the redirect address, and the PKCE verifier. Presenting a code twice revokes everything issued from it.
- Redirect addresses are registered and matched exactly. They must be HTTPS (or loopback HTTP). An unregistered client or address is never redirected to; the user sees an error page.
- The consent page asks for the **connection code** printed by `link`. It is a random 60-bit code stored only as a memory-hard hash, valid for 24 hours, and replaced by `link-code` or `rotate-token`. Five wrong codes lock sign-in for 15 minutes.
- The form is protected against cross-site requests and framing, and the browser that proves the code is the browser that receives the result.
- Access tokens last one hour. Refresh tokens rotate on every use; reusing an old one ends the whole session. Tokens are stored only as hashes. Unlinking a connection or rotating its token revokes every session.
- Each connector is its own issuer. Clients, codes, and tokens from one connector are useless at another.

**Signing in to the MCP provider**
- The owner's provider tokens are stored encrypted with a key kept in an owner-only file next to the database, and are refreshed by the adapter. They are never sent to Muse.
- The sign-in link is single use and expires. Starting it binds the flow to that browser with a cookie, and the provider's answer is accepted only from that browser with the matching state, which defeats attempts to attach someone else's provider account.
- If tokens are missing or cannot be refreshed, requests fail fast with a clear reconnect message (and a fresh link) instead of waiting for a browser. A write is never marked uncertain because of this.

**Write safety**
- By default Muse confirms consequential actions with its user before calling them, so the call itself is the confirmation. An agent that has been fooled by a hidden instruction can skip that step. In `approvalMode: owner` the adapter holds the writes you mark until you approve the exact action on a page, with a passcode the agent never sees: see [Owner approval](owner-approval.md). A signed-approval mode (`approvalMode: signed`) exists in the code but is not usable end to end yet; leave it off.
- Writes carry an idempotency key, supplied by Muse or derived from the request, so a retry returns the stored result instead of running again.
- If the outcome cannot be established (a failure after the request was sent, a rejected result, or a crash), the write is marked `UNKNOWN` and is not retried until you reconcile it. Failures that happen before anything was sent are safely retryable.
- An operation never remains in `RUNNING` after an error.

**Outbound connections**
- Endpoints must use HTTPS, contain no embedded credentials, and resolve only to public addresses. Numeric host tricks (for example `2130706433` or `0x7f.1`) and DNS names that resolve to private addresses are rejected, and the connection is pinned to the addresses that were checked.
- Redirects are not followed. Responses are limited in size and every request has a time limit.
- Resource URIs cannot use schemes such as `file:` or `data:`. HTTP resources must name the hosts they may reach, and template arguments are encoded and can be pattern-restricted.

**Isolation**
- Operations, idempotency keys, tokens, and artifacts are scoped to a connection. A connection can read only its own operations, and a token for one connector is rejected at every other.
- A credential cannot be shared between connections.

**Availability**
- Per-connection rate limits, per-server concurrency limits, request size limits, bounded progress events, and bounded artifacts.
- A server can be disabled instantly with `server-disable`, and a connection revoked with `unlink`.

**Data at rest and audit**
- The database and artifacts are created owner-only. Catalogs can be HMAC-signed so tampering is detected. OAuth tokens are encrypted at rest.
- Administrative actions (who registered, discovered, published, activated, rolled back, linked, or disabled) and every runtime request are recorded. Logs and audit data redact token-like keys and bearer values.

## Your responsibilities

- The policy governs what passes through the adapter. It does not limit what Muse can do with its other tools: a tool the adapter does not publish cannot be called through it, but Muse may offer another route, such as its own browser. Limit that in Muse.

- Provide secrets (downstream tokens, provider client secrets, optional integrity or approval keys) through environment variables or your secret manager, and rotate them.
- Run the service behind TLS and restrict network access to it. The sign-in pages must be served over HTTPS in production.
- Keep the connection code and bearer token private: give them only to Muse, and run `rotate-token` if they leak.
- Review discovery output and drift before publishing, and keep policies least-privilege.
