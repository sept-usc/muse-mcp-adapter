# Data Plane

## Boundary

The data plane is the runtime path between Muse and registered MCP servers. It executes only customer-approved, published catalog definitions.

```text
Muse Custom Connector
        │
        ▼
Ingress and connector routing
        │
        ▼
Published-catalog resolver
        │
        ▼
Policy and request enforcement
        │
        ▼
Primitive dispatcher
   ┌────┼─────────┐
   ▼    ▼         ▼
 Tool Resource  Prompt
   └────┼─────────┘
        ▼
MCP client runtime and auth resolver
        │
        ▼
Registered MCP server
        │
        ▼
Result validation and normalization
        │
        ▼
Muse
```

## Runtime rules

1. Runtime requests never choose arbitrary MCP endpoints.
2. Runtime requests never bypass the published catalog by supplying raw tool or prompt names.
3. Resource URIs must match a published URI or approved template.
4. Credentials are resolved from customer-controlled secret references, never accepted as tool arguments.
5. Input is validated against the published definition, not a fresh live discovery result.
6. All downstream descriptions, prompts, resources, and results are untrusted content.
7. Consequential writes are not retried unless idempotency or status recovery makes the retry safe.

## Components

### Muse ingress and connector router

Terminates the Muse-facing HTTPS API and authenticates the caller. It resolves:

- Connector ID.
- Published catalog version.
- Local user or service identity.
- Downstream MCP server registration.
- Downstream account/workspace connection.
- Public operation name.

The route mapping comes from the compiled contract. Muse cannot substitute a different server ID or endpoint in request arguments.

### Published-catalog resolver

Loads the immutable primitive definition and policy associated with the public operation. It verifies that the catalog is active and that the operation has not been disabled for an emergency reason.

The resolver may cache immutable catalog entries locally. Cache keys include catalog version and primitive ID.

### Policy enforcement

Applies runtime constraints compiled by the control plane:

- Caller and connection access.
- Input schema.
- Tool classification.
- Required approval state.
- Allowed resource URI/template and MIME policy.
- Prompt argument and role restrictions.
- Payload and result-size limits.
- Timeout, concurrency, and rate limits.
- Required downstream scopes.

Runtime policy narrows published access when necessary but never expands it.

### Primitive dispatcher

Routes a resolved primitive to a type-specific executor. Tools, resources, and prompts share authentication, transport, auditing, and result infrastructure but do not share execution semantics.

### Tool executor

The tool executor:

1. Validates arguments against the published input schema.
2. Establishes idempotency and approval context.
3. Resolves downstream credentials.
4. Calls `tools/call` through the MCP runtime.
5. Handles complete, input-required, progress, and error results.
6. Validates structured output against the published output schema.
7. Normalizes content for Muse.

Writes use an operation record before the downstream call begins. An uncertain downstream outcome is recorded as `UNKNOWN`, not treated as failure or retried blindly.

### Resource executor

The resource executor:

1. Resolves an approved static URI or resource template.
2. Validates template arguments and the resulting URI.
3. Calls `resources/read`.
4. Validates returned URI, MIME type, annotations, and size.
5. Marks all content as untrusted external context.
6. Returns inline text or a customer-controlled artifact reference for permitted large/binary content.

It prevents arbitrary file access, internal-network URL access, URI-template escape, oversized results, and unintended recursive resource retrieval.

### Prompt executor

The prompt executor:

1. Resolves an approved prompt definition.
2. Validates prompt arguments.
3. Calls `prompts/get`.
4. Validates returned messages, roles, annotations, embedded resources, and size.
5. Preserves source server, prompt name, and catalog version as provenance.
6. Returns prompt content without authorizing actions suggested by that content.

Prompt retrieval is context retrieval. It is not approval to execute tools mentioned in the prompt.

### MCP client runtime

Provides the common southbound protocol implementation:

- Protocol revision negotiation.
- Streamable HTTP transport.
- Request metadata and JSON-RPC correlation.
- Tool, resource, and prompt operations.
- Pagination and supported caching semantics.
- Progress, input-required, cancellation, and errors.
- Compatibility modules for explicitly supported older revisions.

Provider packs cannot replace the MCP runtime. They only configure it.

### Authentication resolver

Resolves the credential reference for the local user or configured service identity and downstream connection. It obtains or refreshes a resource-bound token where needed and supplies it only to the registered MCP endpoint.

It enforces issuer, audience/resource, scope, account/workspace, and expiry constraints. A token belonging to one connection cannot be reused for another.

### Operation state manager

Maintains durable execution state:

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

The first implementation can complete most reads synchronously. The operation model still exists from the beginning so writes, timeouts, input-required flows, and later task support do not require redesign.

### Result normalizer

Converts MCP results into the stable Muse-facing response contract while preserving provenance and error categories.

It handles:

- Text content.
- Structured JSON.
- Images and audio when permitted.
- Resource links and embedded resources.
- Tool execution errors.
- Protocol and transport errors.
- Input-required results.

Normalization never turns an invalid downstream result into an apparent success.

### Execution audit and telemetry

Records the runtime chain without storing secrets or unnecessary content:

- Muse request/correlation ID.
- Connector and catalog version.
- Local identity and downstream connection reference.
- MCP server and primitive ID.
- Classification and approval reference.
- Timing, result category, and retry behavior.
- Downstream request correlation.

Administrative audit and runtime audit are separate streams.

## Primitive flows

### Tool call

```text
Muse operation
  -> resolve published tool
  -> validate input and approval
  -> create operation/idempotency record
  -> resolve downstream auth
  -> MCP tools/call
  -> validate result
  -> normalize and audit
```

### Resource read

```text
Muse resource request
  -> resolve published URI/template
  -> validate URI and arguments
  -> resolve downstream auth
  -> MCP resources/read
  -> validate content policy
  -> normalize or store artifact
```

### Prompt retrieval

```text
Muse prompt request
  -> resolve published prompt
  -> validate arguments
  -> resolve downstream auth
  -> MCP prompts/get
  -> validate messages and embedded content
  -> return with provenance
```

## Cancellation

Cancellation depends on how far the operation has progressed:

| State when cancelled | Result |
| --- | --- |
| `PENDING`, `APPROVAL_REQUIRED`, `INPUT_REQUIRED` | `CANCELLED` immediately; nothing was dispatched. HTTP `200`, `cancellation: "applied"`. |
| `RUNNING` read | The executing client is signalled to stop; the operation ends `CANCELLED` or `FAILED` (retryable). |
| `RUNNING` write | **Recorded as a request only.** The write may already have taken effect, so the operation keeps running and resolves to its real outcome (`COMPLETED`, `FAILED`, or `UNKNOWN`). HTTP `202`, `cancellation: "requested"`, with a `cancel_requested` audit event. |

A dispatched write is never reported as `CANCELLED`, because that would let a caller believe a side effect did not happen.

## Failure model

Errors remain distinguishable:

- Muse request/authentication error.
- Published-catalog or policy error.
- Downstream authorization/scope error.
- MCP protocol error.
- Tool execution error.
- Resource or prompt content-policy error.
- Transport timeout or unavailability.
- Unknown write outcome.

This distinction lets Muse, the customer administrator, and the operator take the correct recovery action.

## Initial deployment

The first implementation should be a modular gateway service rather than many microservices. Logical modules can run in one process with:

- A relational database or durable local store for catalogs, connections, operations, and audit metadata.
- A customer-controlled secret store for credentials.
- Optional object storage for large resource and result artifacts.
- Structured logs and traces.

Queues, separate workers, and local MCP runners should be introduced only when asynchronous work, isolation, or private connectivity requires them.

## Explicit non-goals for the first data plane

- Arbitrary runtime MCP endpoint registration.
- Automatic exposure of newly discovered primitives.
- Cross-customer routing.
- Local `stdio` execution in the first hosted gateway version.
- Full MCP Apps rendering.
- Blind retry of non-idempotent writes.

