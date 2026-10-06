import re
from collections.abc import Mapping, Sequence
from string import Formatter
from typing import cast
from urllib.parse import quote, urlsplit

from pydantic import AnyUrl

from ..catalog.canonical import freeze_json
from ..credentials.resolver import CredentialResolver
from ..domain.errors import (
    DownstreamContentError,
    OperationNotFoundError,
    RequestValidationError,
    ServerDisabledError,
    UnsupportedPrimitiveError,
)
from ..domain.models import (
    DataPlaneRequest,
    DataPlaneResponse,
    McpContentBlock,
    McpPromptResult,
    McpResourceContent,
    McpResourceResult,
    NormalizedContent,
    NormalizedMessage,
    PublishedPrimitive,
    ResolvedCredential,
)
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from ..policy.validate_policy import FORBIDDEN_URI_SCHEMES
from .content import normalize_block, normalize_resource
from .mcp_client import McpReadClient

_TEMPLATE_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_URI_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
MAX_ARGUMENT_CHARS = 2048


class ReadDataPlaneService:
    def __init__(
        self,
        repository: SqliteControlPlaneRepository,
        credential_resolver: CredentialResolver,
        mcp_client: McpReadClient,
    ) -> None:
        self._repository = repository
        self._credential_resolver = credential_resolver
        self._mcp_client = mcp_client

    def execute(self, request: DataPlaneRequest) -> DataPlaneResponse:
        _validate_request_envelope(request)
        if not self._repository.get_server(request.server_id).enabled:
            raise ServerDisabledError(f"Server {request.server_id} is disabled")
        catalog = self._repository.get_active_catalog(request.server_id)
        operation = next(
            (
                item
                for item in catalog.primitives
                if item.public_operation_id == request.public_operation_id
            ),
            None,
        )
        if operation is None:
            raise OperationNotFoundError(
                f"Operation {request.public_operation_id} is not published for server "
                f"{request.server_id}"
            )
        if operation.primitive_type not in ("resource", "resource_template", "prompt"):
            raise UnsupportedPrimitiveError(
                f"Primitive type {operation.primitive_type} is not available in the read data plane"
            )

        endpoint = _required_string(catalog.server, "endpoint", "Catalog endpoint is missing")
        resource_uri = (
            _resource_uri(operation, request.arguments)
            if operation.primitive_type in ("resource", "resource_template")
            else None
        )
        prompt_arguments = (
            _prompt_arguments(operation, request.arguments)
            if operation.primitive_type == "prompt"
            else None
        )
        credential = self._credential_resolver.resolve(
            local_identity_id=request.local_identity_id,
            connection_id=request.connection_id,
            server_id=request.server_id,
            required_scopes=frozenset(_string_sequence(operation.policy.get("requiredScopes", ()))),
        )

        if operation.primitive_type in ("resource", "resource_template"):
            assert resource_uri is not None
            return self._execute_resource(
                request=request,
                operation=operation,
                catalog_version=catalog.catalog_version,
                endpoint=endpoint,
                credential=credential,
                uri=resource_uri,
            )
        assert prompt_arguments is not None
        return self._execute_prompt(
            request=request,
            operation=operation,
            catalog_version=catalog.catalog_version,
            endpoint=endpoint,
            credential=credential,
            arguments=prompt_arguments,
        )

    def _execute_resource(
        self,
        *,
        request: DataPlaneRequest,
        operation: PublishedPrimitive,
        catalog_version: str,
        endpoint: str,
        credential: ResolvedCredential,
        uri: str,
    ) -> DataPlaneResponse:
        result = self._mcp_client.read_resource(
            endpoint=endpoint,
            uri=uri,
            credential=credential,
        )
        contents = _normalize_resource_result(operation, uri, result)
        return DataPlaneResponse(
            request_id=request.request_id,
            catalog_version=catalog_version,
            public_operation_id=operation.public_operation_id,
            primitive_type=operation.primitive_type,
            provenance=_provenance(request.server_id, operation, catalog_version),
            contents=contents,
        )

    def _execute_prompt(
        self,
        *,
        request: DataPlaneRequest,
        operation: PublishedPrimitive,
        catalog_version: str,
        endpoint: str,
        credential: ResolvedCredential,
        arguments: Mapping[str, str],
    ) -> DataPlaneResponse:
        result = self._mcp_client.get_prompt(
            endpoint=endpoint,
            name=operation.mcp_name,
            arguments=arguments,
            credential=credential,
        )
        messages = _normalize_prompt_result(operation, result)
        return DataPlaneResponse(
            request_id=request.request_id,
            catalog_version=catalog_version,
            public_operation_id=operation.public_operation_id,
            primitive_type=operation.primitive_type,
            provenance=_provenance(request.server_id, operation, catalog_version),
            messages=messages,
        )


def _validate_request_envelope(request: DataPlaneRequest) -> None:
    missing = [
        field
        for field, value in (
            ("request_id", request.request_id),
            ("server_id", request.server_id),
            ("public_operation_id", request.public_operation_id),
            ("local_identity_id", request.local_identity_id),
            ("connection_id", request.connection_id),
        )
        if not value
    ]
    if missing:
        raise RequestValidationError("Missing request fields: " + ", ".join(missing))


def _resource_uri(operation: PublishedPrimitive, arguments: Mapping[str, object]) -> str:
    if operation.primitive_type == "resource":
        if arguments:
            raise RequestValidationError("Static resource operations do not accept arguments")
        uri = _required_string(operation.definition, "uri", "Published resource URI is missing")
        _check_resolved_uri(uri, operation.policy.get("resource"))
        return uri

    template = _required_string(
        operation.definition,
        "uriTemplate",
        "Published resource template is missing",
    )
    patterns = _argument_patterns(operation.policy.get("resource"))
    formatter = Formatter()
    parts: list[str] = []
    fields: set[str] = set()
    try:
        parsed_template = list(formatter.parse(template))
    except ValueError as error:
        raise RequestValidationError("Unsupported resource template syntax") from error
    for literal, field_name, format_spec, conversion in parsed_template:
        parts.append(literal)
        if field_name is None:
            continue
        if not _TEMPLATE_FIELD.fullmatch(field_name) or format_spec or conversion:
            raise RequestValidationError("Unsupported resource template syntax")
        fields.add(field_name)
        value = arguments.get(field_name)
        if not isinstance(value, str) or not value:
            raise RequestValidationError(f"Resource template argument {field_name} is required")
        if (
            value in (".", "..")
            or len(value) > MAX_ARGUMENT_CHARS
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise RequestValidationError(
                f"Resource template argument {field_name} contains an unsafe value"
            )
        _check_pattern(patterns, field_name, value)
        parts.append(quote(value, safe=""))
    extra = set(arguments) - fields
    if extra:
        raise RequestValidationError(
            "Unexpected resource template arguments: " + ", ".join(sorted(extra))
        )
    uri = "".join(parts)
    _check_resolved_uri(uri, operation.policy.get("resource"))
    return uri


def _argument_patterns(policy: object) -> Mapping[str, str]:
    if not isinstance(policy, Mapping):
        return {}
    raw = cast(Mapping[str, object], policy).get("argumentPatterns")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(name): pattern
        for name, pattern in cast(Mapping[object, object], raw).items()
        if isinstance(pattern, str)
    }


def _check_pattern(patterns: Mapping[str, str], name: str, value: str) -> None:
    pattern = patterns.get(name)
    if pattern is not None and re.fullmatch(pattern, value) is None:
        raise RequestValidationError(f"Argument {name} does not match the published constraint")


def _check_resolved_uri(uri: str, resource_policy: object) -> None:
    match = _URI_SCHEME.match(uri)
    if match is None:
        raise RequestValidationError("Resolved resource URI has no scheme")
    scheme = match.group(1).lower()
    if scheme in FORBIDDEN_URI_SCHEMES:
        raise RequestValidationError(f"Resolved resource URI scheme {scheme} is not permitted")
    if isinstance(resource_policy, Mapping):
        policy = cast(Mapping[str, object], resource_policy)
        allowed = policy.get("allowedUriSchemes")
        if (
            isinstance(allowed, Sequence)
            and not isinstance(allowed, str)
            and scheme not in {str(item).lower() for item in cast(Sequence[object], allowed)}
        ):
            raise RequestValidationError(f"Resolved resource URI scheme {scheme} is not allowed")
        allowed_hosts = policy.get("allowedUriHosts")
        if scheme in ("http", "https"):
            host = urlsplit(uri).hostname
            if (
                not isinstance(allowed_hosts, Sequence)
                or isinstance(allowed_hosts, str)
                or host is None
                or host.lower()
                not in {str(item).lower() for item in cast(Sequence[object], allowed_hosts)}
            ):
                raise RequestValidationError("Resolved resource URI host is not allowed")


def _normalize_uri(uri: str) -> str:
    try:
        return str(AnyUrl(uri))
    except ValueError:
        return uri


def _prompt_arguments(
    operation: PublishedPrimitive, arguments: Mapping[str, object]
) -> Mapping[str, str]:
    definitions = operation.definition.get("arguments", ())
    if not _sequence(definitions):
        raise RequestValidationError("Published prompt arguments are invalid")

    allowed: set[str] = set()
    required: set[str] = set()
    for value in cast(Sequence[object], definitions):
        definition = _mapping(value, "Published prompt argument is invalid")
        name = _required_string(definition, "name", "Prompt argument name is missing")
        allowed.add(name)
        if definition.get("required") is True:
            required.add(name)

    missing = required - set(arguments)
    if missing:
        raise RequestValidationError("Missing prompt arguments: " + ", ".join(sorted(missing)))
    extra = set(arguments) - allowed
    if extra:
        raise RequestValidationError("Unexpected prompt arguments: " + ", ".join(sorted(extra)))

    patterns = _argument_patterns(operation.policy.get("prompt"))
    result: dict[str, str] = {}
    for name, value in arguments.items():
        if not isinstance(value, str):
            raise RequestValidationError(f"Prompt argument {name} must be a string")
        if len(value) > MAX_ARGUMENT_CHARS:
            raise RequestValidationError(f"Prompt argument {name} is too long")
        _check_pattern(patterns, name, value)
        result[name] = value
    return result


def _normalize_resource_result(
    operation: PublishedPrimitive,
    requested_uri: str,
    result: McpResourceResult,
) -> tuple[NormalizedContent, ...]:
    resource_policy = _mapping(operation.policy.get("resource"), "Resource policy is missing")
    allowed_mime_types = set(_string_sequence(resource_policy.get("allowedMimeTypes")))
    max_bytes = _positive_int(resource_policy.get("maxBytes"), "Resource maxBytes is invalid")
    normalized: list[NormalizedContent] = []
    total_bytes = 0
    for content in result.contents:
        if _normalize_uri(content.uri) != _normalize_uri(requested_uri):
            raise DownstreamContentError("Resource result URI does not match the requested URI")
        item, size = _normalize_resource_content(content, allowed_mime_types)
        total_bytes += size
        if total_bytes > max_bytes:
            raise DownstreamContentError("Resource result exceeds the published size limit")
        normalized.append(item)
    return tuple(normalized)


def _normalize_prompt_result(
    operation: PublishedPrimitive, result: McpPromptResult
) -> tuple[NormalizedMessage, ...]:
    prompt_policy = _mapping(operation.policy.get("prompt"), "Prompt policy is missing")
    allowed_roles = set(_string_sequence(prompt_policy.get("allowedRoles")))
    allowed_mime_types = set(_string_sequence(prompt_policy.get("allowedEmbeddedMimeTypes")))
    max_bytes = _positive_int(prompt_policy.get("maxBytes"), "Prompt maxBytes is invalid")
    messages: list[NormalizedMessage] = []
    total_bytes = 0

    for message in result.messages:
        if message.role not in allowed_roles:
            raise DownstreamContentError(f"Prompt returned disallowed role {message.role}")
        blocks: list[NormalizedContent] = []
        for block in message.content:
            normalized, size = _normalize_prompt_block(block, allowed_mime_types)
            total_bytes += size
            if total_bytes > max_bytes:
                raise DownstreamContentError("Prompt result exceeds the published size limit")
            blocks.append(normalized)
        messages.append(NormalizedMessage(role=message.role, content=tuple(blocks)))
    return tuple(messages)


def _normalize_prompt_block(
    block: McpContentBlock, allowed_mime_types: set[str]
) -> tuple[NormalizedContent, int]:
    return normalize_block(block, allowed_mime_types, noun="Prompt")


def _normalize_resource_content(
    content: McpResourceContent, allowed_mime_types: set[str]
) -> tuple[NormalizedContent, int]:
    return normalize_resource(content, allowed_mime_types, noun="Content")


def _provenance(
    server_id: str, operation: PublishedPrimitive, catalog_version: str
) -> Mapping[str, object]:
    return cast(
        Mapping[str, object],
        freeze_json(
            {
                "serverId": server_id,
                "catalogVersion": catalog_version,
                "primitiveKey": operation.primitive_key,
                "mcpName": operation.mcp_name,
                "definitionDigest": operation.definition_digest,
            }
        ),
    )


def _mapping(value: object, message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RequestValidationError(message)
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise RequestValidationError(message)
    return cast(Mapping[str, object], mapping)


def _required_string(value: Mapping[str, object], key: str, message: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RequestValidationError(message)
    return item


def _string_sequence(value: object) -> tuple[str, ...]:
    if not _sequence(value):
        raise RequestValidationError("Published string list is invalid")
    items = cast(Sequence[object], value)
    if not all(isinstance(item, str) and item for item in items):
        raise RequestValidationError("Published string list is invalid")
    return tuple(cast(Sequence[str], items))


def _positive_int(value: object, message: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RequestValidationError(message)
    return value


def _sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
