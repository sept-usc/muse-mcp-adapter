# Control Plane

## Boundary

The control plane belongs to one customer-owned adapter deployment. It configures and publishes MCP integrations for that customer; it is not a multi-customer service.

The control plane is not in the normal Muse request path. Its output is an immutable published catalog that the data plane can load and execute.

```text
Customer administrator
  -> register MCP server
  -> connect credentials
  -> discover tools, resources, and prompts
  -> review exposure and Muse policy
  -> compile Muse contract
  -> publish catalog version
  -> inspect drift and audit history
```

## Responsibilities

### MCP server registry

Stores customer-configured MCP server connections:

- Stable local server ID.
- Provider-pack reference, if one is used.
- Remote endpoint or future local-runner reference.
- Transport and supported compatibility profile.
- Authentication profile and secret references.
- Provider account or workspace selection.
- Network and TLS requirements.
- Active published catalog version.

The registry never accepts an arbitrary server URL from a Muse runtime request. Servers are registered administratively before use.

### Authentication setup

Establishes downstream MCP authentication through delegated OAuth, configured OAuth applications, service identities, bearer tokens, or other supported profiles.

The control plane records connection identity, granted scopes, expiry metadata, and references to secrets. Secret values remain in the customer-controlled secret store.

### Discovery and conformance

Connects as an MCP client to discover:

- Server identity and protocol revisions.
- Server capabilities.
- Tools and schemas.
- Resources and resource templates.
- Prompts and argument definitions.

Discovery produces a snapshot. It does not make a primitive available to Muse.

Conformance checks reject malformed schemas, invalid names, unsupported content types, incompatible protocol behavior, and other definitions the data plane cannot execute safely.

### Canonical primitive catalog

Normalizes tools, resources, resource templates, and prompts into distinct internal definitions. Each normalized definition receives a deterministic digest.

The catalog retains both the original MCP definition and the normalized representation used for policy, comparison, contract compilation, and execution.

### Exposure and Muse policy

The customer explicitly decides which primitives Muse may access.

Tool policy includes:

- Allow or deny.
- Read, Write, or Sensitive Write classification.
- Required downstream scopes.
- Timeout, retry, and idempotency behavior.
- Input and output restrictions.

Resource policy includes:

- Allowed static URIs and URI templates.
- Template-argument constraints.
- MIME types and content-size limits.
- Binary-content and caching policy.

Prompt policy includes:

- Allowed prompt names.
- Argument constraints.
- Allowed roles, annotations, and embedded content.
- Whether explicit user selection is required.

MCP annotations inform review but never directly grant access or determine Muse authorization.

### Muse contract compiler

Compiles the approved catalog into the API description consumed by a Muse Custom Connector.

The compiler assigns stable public operation names, request and response schemas, descriptions, Muse permission classifications, adapter routes, and the catalog version.

The recommended default is one Muse Custom Connector contract per MCP server or intentional logical bundle, even though all contracts are served by the same adapter deployment.

### Catalog publication

Publishes an immutable catalog version. The data plane executes only definitions present in that version.

Publishing a new version is explicit. A new discovery snapshot never mutates the active catalog automatically.

### Drift analysis

Compares a fresh discovery snapshot with the published catalog and reports:

- Added or removed primitives.
- Schema changes.
- Description and annotation changes.
- Authentication or scope changes.
- Resource URI or MIME changes.
- Prompt argument, role, or content changes.
- Protocol-capability changes.

The first implementation can run drift analysis manually. Scheduled checks can be added without changing the publication model.

### Administrative audit history

Records configuration changes and publication decisions: who registered a server, connected an account, approved a primitive, changed policy, published a catalog, or accepted/rejected drift.

This is distinct from data-plane execution auditing.

## Provider packs

Provider packs are local, versioned files shipped with the repository or installed by the customer. They supply tested defaults, documentation, policy suggestions, known limitations, discovery fixtures, and conformance tests.

There is no hosted provider-pack marketplace or registry service.

## Minimal first implementation

The first control plane can be a CLI and local administrative API backed by versioned files and a small database. It does not require a management UI.

Required sequence:

1. Register server.
2. Configure authentication.
3. Run discovery.
4. Review all three primitive catalogs.
5. Apply policy.
6. Compile the Muse contract.
7. Publish catalog version.
8. Run a manual drift comparison.
9. Record the administrative audit event.

## Explicit non-goals

- Multi-customer tenancy.
- Billing or subscriptions.
- Hosted provider-pack marketplace.
- Cross-customer administration.
- Runtime tool, resource, or prompt execution.
- Dynamic publication of newly discovered primitives.

