from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PublishedPrimitive:
    primitive_key: str
    primitive_type: str
    mcp_name: str
    public_operation_id: str
    classification: str
    definition_digest: str
    contract_digest: str
    definition: Mapping[str, object]
    policy: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PublishedCatalog:
    catalog_version: str
    catalog_digest: str
    server: Mapping[str, object]
    protocol_version: str
    discovered_at: str
    published_at: str
    primitives: tuple[PublishedPrimitive, ...]


@dataclass(frozen=True, slots=True)
class DriftEvent:
    primitive_key: str
    drift_type: str
    severity: str
    reason: str


@dataclass(frozen=True, slots=True)
class ServerRegistration:
    server_id: str
    display_name: str
    endpoint: str
    auth_profile: str
    provider_pack: str | None
    enabled: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DiscoverySnapshot:
    snapshot_id: int | None
    server_id: str
    protocol_version: str
    discovered_at: str
    snapshot_digest: str
    server_info: Mapping[str, object]
    capabilities: Mapping[str, object]
    primitives: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class CatalogActivation:
    server_id: str
    catalog_version: str
    previous_catalog_version: str | None
    event_type: str
    activated_at: str


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    credential_ref: str
    access_token: str = field(repr=False)
    scopes: frozenset[str] = frozenset()
    local_identity_id: str = ""
    connection_id: str = ""
    server_id: str = ""


@dataclass(frozen=True, slots=True)
class DataPlaneRequest:
    request_id: str
    server_id: str
    public_operation_id: str
    local_identity_id: str
    connection_id: str
    arguments: Mapping[str, object]
    idempotency_key: str | None = None
    approval: "ApprovalEvidence | None" = None
    # Values the adapter filled in from sealed handles (value -> handle). A downstream result that
    # echoes one back is sealed again before the agent sees it.
    filled: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    approval_id: str
    classification: str
    action_digest: str
    approved_at: str
    expires_at: str
    signature: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class McpResourceContent:
    uri: str
    mime_type: str
    text: str | None = None
    blob: bytes | None = None
    annotations: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class McpResourceResult:
    contents: tuple[McpResourceContent, ...]


@dataclass(frozen=True, slots=True)
class McpContentBlock:
    """One MCP content block: text, image, audio, resource link, or embedded resource."""

    block_type: str
    text: str | None = None
    resource: McpResourceContent | None = None
    data: str | None = None  # base64, for image and audio
    mime_type: str | None = None
    uri: str | None = None  # for resource links
    name: str | None = None
    annotations: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class McpPromptMessage:
    role: str
    content: tuple[McpContentBlock, ...]


@dataclass(frozen=True, slots=True)
class McpPromptResult:
    description: str | None
    messages: tuple[McpPromptMessage, ...]


@dataclass(frozen=True, slots=True)
class McpToolResult:
    content: tuple[McpContentBlock, ...]
    structured_content: Mapping[str, object] | None = None
    is_error: bool = False
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedContent:
    kind: str  # text | image | audio | resource | resource_link | artifact
    text: str
    mime_type: str
    uri: str | None
    annotations: Mapping[str, object]
    data: str | None = None  # base64 for binary content (image, audio, binary resources)
    name: str | None = None  # for resource links


@dataclass(frozen=True, slots=True)
class NormalizedMessage:
    role: str
    content: tuple[NormalizedContent, ...]


@dataclass(frozen=True, slots=True)
class DataPlaneResponse:
    request_id: str
    catalog_version: str
    public_operation_id: str
    primitive_type: str
    provenance: Mapping[str, object]
    operation_id: str | None = None
    operation_status: str | None = None
    structured_content: Mapping[str, object] | None = None
    contents: tuple[NormalizedContent, ...] = ()
    messages: tuple[NormalizedMessage, ...] = ()
    replayed: bool = False  # the stored result of an earlier identical request


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    request_id: str
    server_id: str
    catalog_version: str
    public_operation_id: str
    local_identity_id: str
    connection_id: str
    classification: str
    status: str
    idempotency_key: str | None
    request_digest: str
    approval_id: str | None
    created_at: str
    updated_at: str
    result: Mapping[str, object] | None
    error_code: str | None
    error_message: str | None
    retryable: bool


@dataclass(frozen=True, slots=True)
class OperationEvent:
    event_id: int | None
    operation_id: str
    event_type: str
    occurred_at: str
    data: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    operation_id: str
    file_name: str
    media_type: str
    size_bytes: int
    content_digest: str
    storage_path: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ConnectionRecord:
    """One link between a Muse connector and an MCP server, owned by one person."""

    connection_id: str
    slug: str
    token_hash: str
    identity_id: str
    server_id: str
    label: str
    credential_kind: str
    credential_ref: str
    scopes: tuple[str, ...]
    created_at: str
    revoked_at: str | None
    link_code_hash: str | None = None
    link_code_salt: str | None = None
    link_code_expires_at: str | None = None
