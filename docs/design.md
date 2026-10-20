# Muse MCP Adapter — System Design

## 1. Purpose

This is the system-level design for a customer-owned Muse MCP Adapter. It consolidates the product boundary, platform assumptions, architectural decisions, Connector-to-MCP mapping, security model, provider-pack model, validation strategy, roadmap, and open questions.

The detailed logical planes are maintained separately:

- [Control plane](control-plane.md): configure, discover, review, compile, and publish integrations.
- [Data plane](data-plane.md): execute published integrations at runtime.

The first implementation runs both planes in one modular gateway service.

## 2. Problem and objective

Customers increasingly expose application capabilities through MCP servers. Muse integrates services through approved Connectors or user-created Custom Connectors.

Onboarding each MCP server as an unrelated Custom Connector duplicates protocol, authentication, lifecycle, policy, and drift-handling work. It can also change the Muse-facing contract unexpectedly when an MCP server changes.

The adapter provides one governed translation boundary:

```text
Muse Custom Connector
        │ stable HTTPS operations
        ▼
Customer-owned Muse MCP Adapter
        │ MCP protocol
        ▼
Provider-operated or customer-operated MCP servers
```

The adapter must:

- Run inside and be owned by one customer environment.
- Connect that environment to multiple provider or internal MCP servers.
- Act as a conforming MCP client.
- Present a stable, versioned Connector API to Muse.
- Support tools, resources, resource templates, and prompts in the first draft.
- Keep those primitives distinct in the canonical model.
- Separate MCP metadata from customer policy and Muse classification.
- Support delegated-user and service-identity authentication profiles.
- Detect security-relevant and contract-relevant drift.
- Prevent unreviewed MCP changes from expanding Muse capabilities.
- Supply local provider packs and conformance tests as reusable examples.

## 3. Explicit non-goals

- Shared multi-customer SaaS control plane.
- Cross-customer tenancy or routing.
- Billing or subscription management.
- Hosted provider-pack marketplace or registry.
- Automatic publication of discovered primitives.
- Treating MCP annotations as authorization decisions.
- Bypassing Muse approvals or human-in-the-loop controls.
- Recreating the complete MCP host experience inside Muse.
- Full MCP Apps rendering in the first implementation.
- Local `stdio` execution in the first remote-gateway implementation.
- Lossless translation of every optional MCP extension.

The gateway can serve multiple local users, service identities, downstream accounts, and MCP registrations inside the customer boundary. These are isolation domains, not tenants.

## 4. Platform assumptions

### 4.1 Muse

Muse supports approved platform Connectors and user-created Custom Connectors. It classifies Connector operations as Read, Write, or Sensitive Write. An MCP server's annotations cannot replace this Muse-facing classification.

Muse public material describes Connector setup and review expectations but does not provide a complete public Custom Connector wire contract. The accepted API description, authentication flow, payload limits, asynchronous behavior, and approval behavior must be validated through a controlled prototype.

### 4.2 MCP

The design baseline is MCP `2026-07-28` over remote Streamable HTTP. Older revisions belong behind an explicit protocol/transport compatibility interface and are added only for validated customer needs.

The baseline includes JSON-RPC, server discovery, capability negotiation, tools, resources, resource templates, prompts, rich content, structured output, progress, input-required behavior, OAuth for protected HTTP servers, and optional durable tasks.

Server descriptions, schemas, annotations, prompts, resources, and results are untrusted external input.

## 5. Architectural decisions

### Single-customer ownership

Each deployment belongs to one customer. The customer retains custody of endpoints, credentials, policies, catalogs, audit data, and network placement.

### Stable façade over dynamic MCP

Discovery produces a candidate catalog. It never changes the active Muse contract. Only an explicitly reviewed and published immutable catalog is executable.

### Three distinct primitives

- Tools are executable capabilities and may have side effects.
- Resources are URI-addressed context.
- Prompts are user-controlled templates returning messages.

Muse may access resources and prompts through generated Connector operations, but the adapter never converts them into undifferentiated tools internally.

### Provider-neutral core

Provider differences live in declarative packs and tests. Packs cannot replace the MCP client, policy engine, authentication enforcement, or primitive executors.

### Fail closed

Unknown primitives, incompatible changes, missing classifications, invalid resource URIs, insufficient scopes, and uncertain authorization are not executable.

## 6. System architecture

```text
                         CUSTOMER ENVIRONMENT

 Muse Custom Connector
          │ stable, versioned HTTPS contract
          ▼
 ┌───────────────────────────────────────────────────────────┐
 │                 Muse MCP Adapter Gateway                  │
 │                                                           │
 │ Control plane                                             │
 │ register → discover → review → compile → publish          │
 │                                      │                    │
 │                                      ▼                    │
 │                              immutable catalog            │
 │                                      │                    │
 │ Data plane                           ▼                    │
 │ ingress → policy → primitive executor → MCP client        │
 │                         ├─ auth resolver                  │
 │                         ├─ operation state                │
 │                         └─ result/audit                   │
 └───────────────────────────────────────────────┬───────────┘
                                                 │ MCP
                         ┌───────────────────────┼───────────┐
                         ▼                       ▼           ▼
                   Provider MCP             Internal MCP  Other MCP
```

The architecture has three contracts:

| Boundary | Contract |
| --- | --- |
| Muse | Stable operations, Muse authentication, approval context, normalized responses |
| Canonical adapter | Registrations, primitives, policy, identity, operations, drift, audit |
| MCP | Discovery, authorization, primitives, results, lifecycle |

The Muse channel adapter is replaceable. The canonical core remains provider-neutral and mostly consumer-neutral.

## 7. Deployment shape

The initial deployment is a modular monolith:

- One gateway process with logical control-plane and data-plane modules.
- A relational database or durable local store for registrations, catalogs, connections, operations, and audit metadata.
- A customer-controlled secret store for credentials.
- Optional customer-controlled object storage for approved large or binary results.
- Structured logs, metrics, and traces.

The customer places the gateway where it can reach Muse and approved public or private MCP servers. Queues, workers, isolated runners, and `stdio` support are introduced only when asynchronous work or network isolation requires them.

## 8. Connector and server granularity

The default is one Muse Custom Connector contract per MCP server or intentional logical bundle. Muse never receives a generic unrestricted operation such as `invoke(server, primitive, arguments)`.

Each published Muse operation compiles to a fixed mapping:

```text
connector ID
catalog version
public operation ID
MCP server registration ID
primitive type and name
approved schema digest
policy classification
required authorization profile and scopes
result and lifecycle policy
```

This retains shared runtime code without making the adapter an arbitrary MCP proxy.

## 9. Canonical domain model

### Server registration

- Stable server ID and display name.
- Exact endpoint or approved endpoint template.
- Transport and supported protocol revisions.
- Provider pack and deployment profile.
- Authentication profile and secret references.
- TLS, redirect, and network policy.
- Downstream account or workspace reference.
- Discovery snapshot and health state.

### Primitive definition

- Server ID, primitive type, MCP name, and stable public name.
- Raw and normalized definitions.
- Input/output schemas where applicable.
- Resource URI/template, MIME rules, and size limits.
- Prompt arguments, roles, annotations, and embedded-content policy.
- Canonical digest and required scopes.
- Muse classification and approval behavior.
- Idempotency, timeout, retry, and lifecycle policy.
- Publication and deprecation state.

### Published catalog

A catalog is immutable and versioned. It contains the exact definitions the data plane may execute. New discovery results create candidates for a later catalog rather than mutating the active one.

### Operation

```text
PENDING
RUNNING
INPUT_REQUIRED
APPROVAL_REQUIRED
COMPLETED
FAILED
CANCELLED
UNKNOWN
```

`UNKNOWN` represents a consequential request whose downstream outcome cannot be established. It must be reconciled before retry.

## 10. Primitive projection

| MCP primitive | Canonical behavior | Muse-facing projection |
| --- | --- | --- |
| Tool | Validate schema and invoke an action | Individually generated, classified operation |
| Resource | Resolve approved URI and read typed content | Catalog/search/read operation with URI, MIME, size, and access policy |
| Resource template | Validate arguments and build approved URI | Explicit parameterized resource operation |
| Prompt | Retrieve named messages with validated arguments | Catalog/get operation preserving roles, provenance, and content |

Prompt retrieval supplies context. It never authorizes tool calls mentioned in the prompt.

## 11. Connector-to-MCP gap

| Area | Muse Connector | Native MCP | Adapter responsibility |
| --- | --- | --- | --- |
| Contract | Defined service operations | Dynamic capabilities | Stable, versioned façade |
| Discovery | Setup-time definition | Capability and primitive discovery | Snapshot, validate, allowlist, publish |
| Transport | HTTPS API | Streamable HTTP or `stdio` | Terminate Muse API and originate MCP |
| Versioning | Connector lifecycle | Protocol negotiation | Isolate compatibility |
| Tools | Classified operations | JSON Schema tools | Compile schemas and calls |
| Resources | No documented first-class equivalent | List/template/read/subscribe | Preserve semantics through controlled reads |
| Prompts | No documented first-class equivalent | List/get | Preserve semantics through explicit retrieval |
| Permissions | Read/Write/Sensitive Write | Advisory annotations | Apply independent customer policy |
| Authentication | Connector credentials | Resource-bound OAuth or tokens | Maintain two authentication boundaries |
| Progress/input | Muse interaction model | Progress and input-required | Persist and translate lifecycle |
| Long-running work | Connector-specific | Optional Tasks | Durable adapter operations |
| Errors | Service-specific | JSON-RPC and execution errors | Preserve category and recovery data |
| Results | Connector response | Rich and structured content | Validate and normalize safely |
| Governance | Reviewed or configured integration | Open server ecosystem | Block unreviewed expansion |

## 12. Provider packs

A provider pack is a versioned set of local files shipped with the repository or installed by the customer. It is not a hosted marketplace item.

It may define endpoint templates, supported deployments and revisions, authentication profiles, tested discovery snapshots, default allowlists, Muse classifications, resource and prompt policies, required scopes, idempotency behavior, limitations, and conformance/security/drift tests.

```text
providers/
  linear/
    provider.yaml
    policy.yaml
    schemas/
    examples/
    tests/
  atlassian/
  supabase/
```

Configuration resolves from least to most specific:

```text
adapter defaults
  < provider-pack defaults
  < customer deployment policy
  < server registration
  < local identity authorization context
```

Higher layers may narrow access. Broadening access to an unclassified or sensitive capability requires review and publication.

## 13. Server onboarding

1. Select a reference pack or generic template.
2. Register the exact endpoint and deployment profile.
3. Configure authentication and secret references.
4. Discover identity, capabilities, tools, resources, templates, and prompts.
5. Validate protocol behavior and normalize schemas.
6. Compare discovery with the tested pack snapshot.
7. Select and classify exposed primitives.
8. Configure URI, content, scope, timeout, retry, and lifecycle policy.
9. Run conformance and safety tests.
10. Compile the Muse-facing contract.
11. Publish an immutable catalog.
12. Monitor future discovery for drift without changing the active contract.

## 14. Authentication and identity

There are two independent authentication relationships:

1. Muse authenticates to the adapter.
2. The adapter authenticates to an MCP server for a local user or service identity.

The adapter never forwards arbitrary Muse credentials downstream. MCP tokens are bound to the correct issuer, resource/audience, scopes, local identity, connection, and account.

Protected remote server support should include Protected Resource Metadata, authorization-server discovery, Authorization Code with PKCE, configured or standards-based client registration, resource indicators, least-privilege scopes, runtime scope challenges, reauthorization, issuer/audience validation, and confidential refresh-token handling.

Credential values stay in the customer secret store. Catalogs and packs contain only references and non-secret metadata.

## 15. Policy and approval

MCP annotations are evidence, not policy:

- Unclassified primitives are not published.
- Missing or false `readOnlyHint` is Write until reviewed.
- `readOnlyHint: true` is only a candidate Read classification.
- A tool that declares `destructiveHint: true`, or whose name contains a word such as delete, remove, drop, merge, revoke, or reset, is drafted as Sensitive Write.
- External communication, destructive actions, payments, permission changes, credential operations, and irreversible actions require explicit policy.
- Sensitive Writes require the strongest Muse-supported approval behavior.
- Material changes to recipients, price, scope, or other approved details require fresh approval.

Runtime policy may narrow access for an identity, connection, or emergency disablement. It may never expand the published catalog.

## 16. Security model

```text
Muse user
  → Muse credential and approval boundary
  → adapter ingress and policy
  → MCP authorization server
  → MCP resource server
  → downstream application
```

Required controls include:

- Validate input against the published schema, not fresh discovery.
- Validate structured output and enforce size limits.
- Restrict endpoints, redirects, URI schemes, resource templates, and outbound destinations.
- Prevent SSRF and arbitrary file access.
- Validate TLS and Streamable HTTP origins.
- Isolate tokens, caches, operations, and artifacts by identity and connection.
- Redact secrets from logs and encrypt sensitive state at rest.
- Revoke credentials on disconnect.
- Treat all downstream content as untrusted.
- Use idempotency and reconcile uncertain writes before retry.
- Rate-limit by identity, connection, server, and primitive.
- Apply timeouts and cancellation.

Priority threats are malicious descriptions/results, schema changes after approval, falsely read-only tools, confused-deputy and OAuth mix-up, cross-connection leakage, prompt injection, resource-based SSRF, duplicate writes, oversized content, secret leakage, and endpoint takeover.

## 17. Drift and versioning

| Drift | Example | Response |
| --- | --- | --- |
| Schema | Inputs or outputs change | New digest; block incompatibility; review |
| Capability | Primitive added/removed | Record candidate; never auto-publish |
| Permission | Read gains side effects | Fail closed; reclassify |
| Authorization | Scope, issuer, resource, or mode changes | Invalidate assumptions; reauthorize |
| Protocol | Revision or transport changes | Compatibility boundary; preserve public contract |
| Result | New type, larger result, invalid schema | Enforce content policy; block unsafe output |
| Lifecycle | Sync call becomes input-required/task-based | Pause and surface state |

Breaking changes produce a new public contract version. Compatible changes are republished only after validation and policy review.

## 18. Errors, audit, and observability

Errors remain distinguishable: Muse request/authentication, catalog/policy, downstream authorization, MCP protocol, tool execution, resource/prompt content policy, transport failure, and unknown write outcome. Errors carry safe recovery metadata and are never flattened into apparent success.

Administrative audit covers registration, discovery, policy, publication, rollback, emergency disablement, and drift acknowledgement. Runtime audit covers request ID, catalog version, local identity, downstream connection, server and primitive, classification, approval reference, timing, result, retry decision, and downstream correlation.

Logs, metrics, traces, and audits share correlation IDs but exclude tokens and unnecessary content.

## 19. Reference validation

A reference server is added when it introduces a new architectural pressure or strong customer demand. Before implementation, verify the live Muse Connector list so the adapter does not duplicate an adequate native integration.

The first reference is a deterministic conformance server exposing a read tool, idempotent write tool, static resource, resource template, text/binary metadata, and parameterized prompt. It also produces controlled schema, catalog, authorization, result, and lifecycle changes.

| Wave | Providers | Coverage |
| --- | --- | --- |
| 1 | Linear, Atlassian, Supabase | Remote OAuth, progressive catalogs, enterprise permissions, project scoping, risky data operations, input-required behavior |
| 2 | HubSpot, Airtable, Sentry, Cloudflare | Custom OAuth apps, fine-grained scope, live drift, hybrid hosting, code-mode execution |
| 3 | Intercom, MongoDB, Datadog, Grafana, Elastic | Support data, databases, regional endpoints, private networks, transport diversity, per-space isolation |

These are candidates, not claims that Muse lacks native support. Each pack must include reproducible setup, snapshots, reviewed policies, read/write paths, auth failure tests, drift tests, isolation tests, content-safety tests, workflow examples, limitations, and rollback instructions.

## 20. Success criteria

- An operator can register a remote server and inspect validated capabilities.
- Only published primitives can be invoked from Muse.
- Tools, resources, templates, and prompts retain canonical semantics.
- Inputs and structured outputs are schema-validated.
- Resource URI, MIME, size, and access rules are enforced.
- Prompt roles, arguments, provenance, and embedded content are preserved.
- Permission-relevant drift blocks execution until reviewed.
- Credentials cannot cross identities, accounts, connections, or servers.
- Consequential writes are not duplicated through unsafe retries.
- Errors preserve actionable category and recovery information.
- Multiple provider packs use one provider-neutral core.

## 21. Open questions and evidence

### Muse contract

- What API description does Muse accept or generate?
- How are schemas, authentication, and secrets represented?
- What content types, limits, timeouts, and async patterns are supported?
- Can a Connector request approval, or is approval inferred from classification?
- How are Custom Connector contracts updated after schema changes?
- What projection gives resources and prompts the clearest UX?

### Compatibility and operations

- Which target servers need older revisions, legacy transports, or static keys?
- Which optional extensions are required by real workflows?
- Where should the gateway run to reach private MCP servers?
- What availability, latency, concurrency, and duration targets apply?
- Where and how long should large artifacts and audit records be retained?
- Which drift events block execution versus warn?
- Which customer roles may classify and publish primitives?

Required evidence includes a recorded Custom Connector creation, redacted generated definition, permitted request/response traces, approval behavior for all classifications, failure and size-limit behavior, expired-credential behavior, and behavior after upstream schema changes.

## 23. Sources

### Muse

- [Muse Connector Platform](https://muse.ai/platform)
- [Muse Connector guidelines](https://muse.ai/platform/docs)
- [How Muse works with Connectors](https://www.meta.com/help/artificial-intelligence/1687253048996149/)
- [Muse for Small Business](https://muse.ai/business)

### MCP

- [MCP architecture](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture)
- [MCP base protocol](https://modelcontextprotocol.io/specification/2026-07-28/basic/index)
- [MCP transports](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)
- [MCP authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)

### Candidate servers

- [Linear](https://linear.app/docs/mcp)
- [Atlassian](https://atlassian.github.io/atlassian-mcp-server/)
- [Supabase](https://supabase.com/docs/guides/ai-tools/mcp)
- [HubSpot](https://developers.hubspot.com/docs/apps/developer-platform/build-apps/integrate-with-the-remote-hubspot-mcp-server)
- [Airtable](https://support.airtable.com/articles/9897799762-using-the-airtable-mcp-server)
- [Sentry](https://github.com/getsentry/toolkit/blob/main/packages/mcp-core/README.md)
- [Cloudflare](https://developers.cloudflare.com/agents/model-context-protocol/cloudflare/servers-for-cloudflare/)
- [Intercom](https://developers.intercom.com/docs/guides/mcp)
- [MongoDB](https://www.mongodb.com/docs/mcp-server/overview/)
- [Datadog](https://github.com/datadog-labs/mcp-server)
- [Grafana](https://grafana.com/docs/grafana/latest/developer-resources/mcp/configure/)
- [Elastic](https://www.elastic.co/docs/explore-analyze/ai-features/agent-builder/mcp-server)
