from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Event
from typing import Any, Protocol, cast

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import AnyUrl

from ..catalog.canonical import digest, freeze_json
from ..data_plane.content import DEFAULT_TOOL_RESULT_TYPES
from ..data_plane.tool_client import (
    McpCancelled,
    McpInputRequired,
    McpPreDispatchError,
    McpReauthorizationRequired,
    McpToolExecutionFailure,
    McpUnknownOutcome,
)
from ..domain.errors import (
    InvalidStateError,
    ReauthorizationRequiredError,
    UnsafeEndpointError,
)
from ..domain.models import (
    DiscoverySnapshot,
    McpContentBlock,
    McpPromptMessage,
    McpPromptResult,
    McpResourceContent,
    McpResourceResult,
    McpToolResult,
    ResolvedCredential,
    ServerRegistration,
)
from .security import EndpointSecurityPolicy, ResolvedEndpoint

ProgressCallback = Callable[[float, float | None, str | None], None]
ElicitationCallback = Callable[[Any, Any], Any]
LogCallback = Callable[[str, str], None]

IDEMPOTENCY_META_KEY = "io.muse.mcp-adapter/idempotencyKey"
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# JSON-RPC errors by which a server definitively refused the request before acting on it.
_DEFINITIVE_REJECTIONS = frozenset({-32700, -32600, -32601, -32602})


@dataclass(frozen=True, slots=True)
class ConnectedSession:
    session: ClientSession
    protocol_version: str
    server_info: Mapping[str, object] | None = None
    capabilities: Mapping[str, object] | None = None


class SessionFactory(Protocol):
    def connect(
        self,
        endpoint: str,
        credential: ResolvedCredential | None,
        elicitation_callback: ElicitationCallback | None = None,
        logging_callback: Any = None,
    ) -> AbstractAsyncContextManager[ConnectedSession]: ...


class _LimitedStream(httpx.AsyncByteStream):
    """Aborts a response body that grows beyond the configured byte limit."""

    def __init__(self, stream: httpx.AsyncByteStream, limit: int) -> None:
        self._stream = stream
        self._limit = limit

    async def __aiter__(self):  # type: ignore[override]
        received = 0
        async for chunk in self._stream:
            received += len(chunk)
            if received > self._limit:
                raise httpx.StreamError("Downstream response exceeded the size limit")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


def _response_limit_hook(limit: int) -> Callable[[httpx.Response], Any]:
    async def hook(response: httpx.Response) -> None:
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await response.aclose()
            raise httpx.StreamError("Downstream response exceeded the size limit")
        if isinstance(response.stream, httpx.AsyncByteStream):
            response.stream = _LimitedStream(response.stream, limit)

    return hook


class _PinnedAsyncTransport(httpx.AsyncBaseTransport):
    """Connects to a policy-checked IP while retaining the original Host and TLS SNI."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        policy: EndpointSecurityPolicy,
        initial_endpoint: str,
        initial_target: ResolvedEndpoint,
    ) -> None:
        self._inner = inner
        self._policy = policy
        parsed = httpx.URL(initial_endpoint)
        self._targets: dict[tuple[str, str, int], ResolvedEndpoint] = {
            (
                parsed.scheme,
                initial_target.hostname,
                parsed.port or initial_target.port,
            ): initial_target
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if not host:
            raise UnsafeEndpointError("Outbound request has no host")
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        key = (request.url.scheme, host.lower().rstrip("."), port)
        target = self._targets.get(key)
        if target is None:
            origin = f"{request.url.scheme}://{host}:{port}"
            target = await anyio.to_thread.run_sync(self._policy.resolve_endpoint, origin)
            self._targets[key] = target
        address = target.addresses[0]
        headers = request.headers.copy()
        default_port = 443 if request.url.scheme == "https" else 80
        host_header = host if port == default_port else f"{host}:{port}"
        headers["Host"] = host_header
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = host
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()


class StreamableHttpSessionFactory:
    """Creates short-lived, stateless MCP sessions over Streamable HTTP."""

    def __init__(
        self,
        *,
        auth_factory: Callable[[str, ResolvedCredential | None], httpx.Auth | None] | None = None,
        timeout_seconds: float = 30.0,
        endpoint_policy: EndpointSecurityPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        lifespan_factory: Callable[[], AbstractAsyncContextManager[object]] | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._max_response_bytes = max_response_bytes
        self._auth_factory = auth_factory
        self._timeout_seconds = timeout_seconds
        self._endpoint_policy = endpoint_policy or EndpointSecurityPolicy()
        self._transport = transport
        self._lifespan_factory = lifespan_factory

    @asynccontextmanager
    async def connect(
        self,
        endpoint: str,
        credential: ResolvedCredential | None,
        elicitation_callback: ElicitationCallback | None = None,
        logging_callback: Any = None,
    ) -> AsyncGenerator[ConnectedSession]:
        # Resolve once, validate all returned addresses, and pin the transport to the checked IP.
        target = await anyio.to_thread.run_sync(self._endpoint_policy.resolve_endpoint, endpoint)
        auth = self._auth_factory(endpoint, credential) if self._auth_factory else None
        headers: dict[str, str] = {}
        if credential is not None and auth is None and credential.access_token:
            headers["Authorization"] = f"Bearer {credential.access_token}"
        lifespan = (
            self._lifespan_factory() if self._lifespan_factory is not None else _empty_lifespan()
        )
        transport = self._transport
        if transport is None:
            transport = _PinnedAsyncTransport(
                httpx.AsyncHTTPTransport(), self._endpoint_policy, endpoint, target
            )
        async with (
            lifespan,
            httpx.AsyncClient(
                auth=auth,
                headers=headers,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                transport=transport,
                event_hooks={"response": [_response_limit_hook(self._max_response_bytes)]},
            ) as http_client,
            streamable_http_client(
                endpoint,
                http_client=http_client,
                terminate_on_close=True,
            ) as (read_stream, write_stream, _),
            ClientSession(
                read_stream,
                write_stream,
                # Bound every request: an aborted or stalled response must fail, not wait forever.
                read_timeout_seconds=timedelta(seconds=self._timeout_seconds),
                elicitation_callback=cast(Any, elicitation_callback),
                logging_callback=logging_callback,
            ) as session,
        ):
            initialized = await session.initialize()
            yield ConnectedSession(
                session,
                str(initialized.protocolVersion),
                server_info=cast(
                    Mapping[str, object],
                    initialized.serverInfo.model_dump(by_alias=True, exclude_none=True),
                ),
                capabilities=cast(
                    Mapping[str, object],
                    initialized.capabilities.model_dump(by_alias=True, exclude_none=True),
                ),
            )


class InMemorySessionFactory:
    """Real MCP protocol session for deterministic conformance tests."""

    def __init__(self, server: Any) -> None:
        self._server = server

    def connect(
        self,
        endpoint: str,
        credential: ResolvedCredential | None,
        elicitation_callback: ElicitationCallback | None = None,
        logging_callback: Any = None,
    ) -> AbstractAsyncContextManager[ConnectedSession]:
        del endpoint, credential, logging_callback
        return _in_memory_connection(self._server, elicitation_callback)


@dataclass(slots=True)
class _DispatchState:
    """Tracks whether the request left the adapter, to classify failures safely."""

    dispatched: bool = False
    input_required: tuple[str, Mapping[str, object]] | None = None


class McpSdkClient:
    """Provider-neutral adapter over the official MCP Python client SDK."""

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        *,
        supported_protocol_versions: frozenset[str] | None = None,
        discovery_credential: ResolvedCredential | None = None,
    ) -> None:
        self._sessions = session_factory or StreamableHttpSessionFactory()
        # Servers answer an initialize request with the newest revision they support, so
        # accept every revision the SDK can speak unless the deployment narrows the list.
        self._supported_versions = supported_protocol_versions or frozenset(
            SUPPORTED_PROTOCOL_VERSIONS
        )
        self._discovery_credential = discovery_credential

    def discover(self, server: ServerRegistration) -> DiscoverySnapshot:
        return _run(partial(self._discover, server))

    def read_resource(
        self,
        *,
        endpoint: str,
        uri: str,
        credential: ResolvedCredential,
    ) -> McpResourceResult:
        return _run(partial(self._read_resource, endpoint, uri, credential))

    def get_prompt(
        self,
        *,
        endpoint: str,
        name: str,
        arguments: Mapping[str, str],
        credential: ResolvedCredential,
    ) -> McpPromptResult:
        return _run(partial(self._get_prompt, endpoint, name, dict(arguments), credential))

    def call_tool(
        self,
        *,
        endpoint: str,
        name: str,
        arguments: Mapping[str, object],
        credential: ResolvedCredential,
        idempotency_key: str | None,
        timeout_seconds: int | None = None,
        progress_callback: ProgressCallback | None = None,
        elicitation_answer: Mapping[str, object] | None = None,
        cancellation: Event | None = None,
        log_callback: LogCallback | None = None,
    ) -> McpToolResult:
        metadata = {IDEMPOTENCY_META_KEY: idempotency_key} if idempotency_key else None
        state = _DispatchState()
        try:
            return anyio.run(
                partial(
                    self._call_tool,
                    endpoint,
                    name,
                    _plain(arguments),
                    credential,
                    metadata,
                    timeout_seconds,
                    progress_callback,
                    state,
                    dict(elicitation_answer) if elicitation_answer is not None else None,
                    cancellation,
                    log_callback,
                )
            )
        except BaseException as raised:
            if isinstance(raised, KeyboardInterrupt | SystemExit):
                raise
            error = _unwrap(raised)
            if isinstance(
                error,
                McpCancelled
                | McpInputRequired
                | McpToolExecutionFailure
                | McpReauthorizationRequired,
            ):
                raise error from None
            if not state.dispatched:
                raise McpPreDispatchError(
                    f"MCP request was not sent: {type(error).__name__}"
                ) from error
            if isinstance(error, McpError) and error.error.code in _DEFINITIVE_REJECTIONS:
                raise McpToolExecutionFailure(
                    error.error.message, code=f"JSONRPC_{error.error.code}"
                ) from error
            if isinstance(error, TimeoutError):
                raise McpUnknownOutcome("MCP tool call timed out after dispatch") from error
            raise McpUnknownOutcome("MCP transport ended before a result was received") from error

    async def _discover(self, server: ServerRegistration) -> DiscoverySnapshot:
        async with self._sessions.connect(server.endpoint, self._discovery_credential) as connected:
            session = connected.session
            protocol_version = connected.protocol_version
            self._ensure_protocol(protocol_version)
            tools = await _all_pages(session.list_tools, "tools")
            resources = await _all_pages(session.list_resources, "resources")
            templates = await _all_pages(session.list_resource_templates, "resourceTemplates")
            prompts = await _all_pages(session.list_prompts, "prompts")
            primitives = tuple(
                [_tool_primitive(item) for item in tools]
                + [_resource_primitive(item) for item in resources]
                + [_template_primitive(item) for item in templates]
                + [_prompt_primitive(item) for item in prompts]
            )
            capabilities: dict[str, object] = {
                "negotiated": dict(connected.capabilities or {}),
                "tools": bool(tools),
                "resources": bool(resources or templates),
                "prompts": bool(prompts),
            }
            server_info: dict[str, object] = dict(
                connected.server_info or {"name": server.display_name, "version": "unknown"}
            )
            discovered_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            payload = {
                "serverId": server.server_id,
                "protocolVersion": protocol_version,
                "serverInfo": server_info,
                "capabilities": capabilities,
                "primitives": primitives,
            }
            return DiscoverySnapshot(
                snapshot_id=None,
                server_id=server.server_id,
                protocol_version=protocol_version,
                discovered_at=discovered_at,
                snapshot_digest=digest(payload),
                server_info=cast(Mapping[str, object], freeze_json(server_info)),
                capabilities=cast(Mapping[str, object], freeze_json(capabilities)),
                primitives=tuple(
                    cast(Mapping[str, object], freeze_json(item)) for item in primitives
                ),
            )

    async def _read_resource(
        self, endpoint: str, uri: str, credential: ResolvedCredential
    ) -> McpResourceResult:
        async with self._sessions.connect(endpoint, credential) as connected:
            session = connected.session
            self._ensure_protocol(connected.protocol_version)
            result = await session.read_resource(AnyUrl(uri))
            return McpResourceResult(
                contents=tuple(_resource_content(item) for item in result.contents)
            )

    async def _get_prompt(
        self,
        endpoint: str,
        name: str,
        arguments: dict[str, str],
        credential: ResolvedCredential,
    ) -> McpPromptResult:
        async with self._sessions.connect(endpoint, credential) as connected:
            session = connected.session
            self._ensure_protocol(connected.protocol_version)
            result = await session.get_prompt(name, arguments)
            return McpPromptResult(
                description=result.description,
                messages=tuple(
                    McpPromptMessage(
                        role=str(message.role),
                        content=(_content_block(message.content),),
                    )
                    for message in result.messages
                ),
            )

    async def _call_tool(
        self,
        endpoint: str,
        name: str,
        arguments: dict[str, object],
        credential: ResolvedCredential,
        metadata: dict[str, str] | None,
        timeout_seconds: int | None,
        callback: ProgressCallback | None,
        state: _DispatchState,
        elicitation_answer: dict[str, object] | None,
        cancellation: Event | None,
        log_callback: LogCallback | None,
    ) -> McpToolResult:
        async def on_log(params: types.LoggingMessageNotificationParams) -> None:
            if log_callback is not None:
                log_callback(str(params.level), str(params.data))

        async def progress(progress: float, total: float | None, message: str | None) -> None:
            if callback is not None:
                callback(progress, total, message)

        async def elicit(_context: Any, params: Any) -> types.ElicitResult:
            if elicitation_answer is not None:
                return types.ElicitResult(action="accept", content=cast(Any, elicitation_answer))
            schema = getattr(params, "requestedSchema", None)
            state.input_required = (
                str(getattr(params, "message", "Input required")),
                cast(Mapping[str, object], schema if isinstance(schema, dict) else {}),
            )
            return types.ElicitResult(action="cancel")

        async def cancel_on_signal(scope: anyio.CancelScope) -> None:
            assert cancellation is not None
            await anyio.to_thread.run_sync(cancellation.wait, abandon_on_cancel=True)
            scope.cancel()

        async with self._sessions.connect(endpoint, credential, elicit, on_log) as connected:
            session = connected.session
            self._ensure_protocol(connected.protocol_version)
            timeout = timedelta(seconds=timeout_seconds) if timeout_seconds else None
            if cancellation is not None and cancellation.is_set():
                raise McpCancelled("Operation was cancelled before dispatch")
            state.dispatched = True
            result: types.CallToolResult | None = None
            with anyio.CancelScope() as call_scope:
                async with anyio.create_task_group() as tasks:
                    if cancellation is not None:
                        tasks.start_soon(cancel_on_signal, call_scope)
                    try:
                        result = await session.call_tool(
                            name,
                            arguments,
                            read_timeout_seconds=timeout,
                            progress_callback=progress if callback else None,
                            meta=metadata,
                        )
                    finally:
                        tasks.cancel_scope.cancel()
            if cancellation is not None and cancellation.is_set():
                raise McpCancelled("Operation was cancelled")
            if result is None:
                raise McpUnknownOutcome("MCP call ended without a result")
            if state.input_required is not None:
                message, schema = state.input_required
                raise McpInputRequired(message, schema=schema)
            return McpToolResult(
                content=tuple(_content_block(item) for item in result.content),
                structured_content=result.structuredContent,
                is_error=result.isError,
                error_code="MCP_TOOL_ERROR" if result.isError else None,
                error_message=_error_text(result) if result.isError else None,
            )

    def _ensure_protocol(self, version: str | None) -> None:
        if version is None or version not in self._supported_versions:
            raise InvalidStateError(
                f"Negotiated MCP protocol {version!r} is not in the deployment allowlist"
            )


def _run[T](function: Callable[[], Awaitable[T]]) -> T:
    """Run an async client call, surfacing the meaningful error from SDK task groups."""
    try:
        return anyio.run(function)
    except BaseException as raised:
        if isinstance(raised, KeyboardInterrupt | SystemExit):
            raise
        leaf = _unwrap(raised)
        if isinstance(leaf, McpReauthorizationRequired):
            raise ReauthorizationRequiredError(str(leaf)) from None
        if isinstance(leaf, UnsafeEndpointError):
            raise leaf from None
        raise


def _unwrap(error: BaseException) -> BaseException:
    """Return the most informative leaf of an exception group."""
    if isinstance(error, BaseExceptionGroup):
        leaves = [_unwrap(item) for item in cast(Any, error).exceptions]
        for leaf in leaves:
            if isinstance(
                leaf,
                McpInputRequired
                | McpToolExecutionFailure
                | McpReauthorizationRequired
                | UnsafeEndpointError,
            ):
                return leaf
        for leaf in leaves:
            if isinstance(leaf, McpError):
                return leaf
        return leaves[0]
    return error


def _plain(value: object) -> Any:
    """Convert frozen mappings/tuples into plain JSON containers for serialization."""
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _plain(item) for key, item in mapping.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in cast(Sequence[object], value)]
    return value


async def _all_pages(method: Any, field: str) -> list[Any]:
    items: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        page = await method(cursor)
        items.extend(getattr(page, field))
        cursor = page.nextCursor
        if cursor is None:
            return items
        if cursor in seen:
            raise InvalidStateError("MCP server returned a repeating pagination cursor")
        seen.add(cursor)


@asynccontextmanager
async def _in_memory_connection(
    server: Any, elicitation_callback: ElicitationCallback | None = None
) -> AsyncGenerator[ConnectedSession]:
    async with create_connected_server_and_client_session(
        server, elicitation_callback=cast(Any, elicitation_callback)
    ) as session:
        yield ConnectedSession(session, LATEST_PROTOCOL_VERSION)


@asynccontextmanager
async def _empty_lifespan() -> AsyncGenerator[None]:
    yield None


def _tool_primitive(tool: types.Tool) -> Mapping[str, object]:
    task_support = tool.execution.taskSupport if tool.execution is not None else None
    value: dict[str, object] = {
        "type": "tool",
        "name": tool.name,
        "inputSchema": tool.inputSchema,
        "resultContentTypes": list(DEFAULT_TOOL_RESULT_TYPES),
        "lifecycleModes": (
            ["synchronous", "task"] if task_support in ("optional", "required") else ["synchronous"]
        ),
    }
    if tool.title is not None:
        value["title"] = tool.title
    if tool.description is not None:
        value["description"] = tool.description
    if tool.outputSchema is not None:
        value["outputSchema"] = tool.outputSchema
    if tool.annotations is not None:
        value["annotations"] = tool.annotations.model_dump(by_alias=True, exclude_none=True)
    return value


def _resource_primitive(resource: types.Resource) -> Mapping[str, object]:
    value: dict[str, object] = {
        "type": "resource",
        "name": resource.name,
        "uri": str(resource.uri),
    }
    if resource.description is not None:
        value["description"] = resource.description
    if resource.mimeType is not None:
        value["mimeType"] = resource.mimeType
    return value


def _template_primitive(template: types.ResourceTemplate) -> Mapping[str, object]:
    value: dict[str, object] = {
        "type": "resource_template",
        "name": template.name,
        "uriTemplate": template.uriTemplate,
    }
    if template.description is not None:
        value["description"] = template.description
    if template.mimeType is not None:
        value["mimeType"] = template.mimeType
    return value


def _prompt_primitive(prompt: types.Prompt) -> Mapping[str, object]:
    value: dict[str, object] = {
        "type": "prompt",
        "name": prompt.name,
        "arguments": [
            item.model_dump(by_alias=True, exclude_none=True) for item in (prompt.arguments or [])
        ],
    }
    if prompt.description is not None:
        value["description"] = prompt.description
    return value


def _resource_content(
    item: types.TextResourceContents | types.BlobResourceContents,
    annotations: Mapping[str, object] | None = None,
) -> McpResourceContent:
    if isinstance(item, types.TextResourceContents):
        return McpResourceContent(
            uri=str(item.uri),
            mime_type=item.mimeType or "text/plain",
            text=item.text,
            annotations=annotations,
        )
    return McpResourceContent(
        uri=str(item.uri),
        mime_type=item.mimeType or "application/octet-stream",
        blob=item.blob.encode(),
        annotations=annotations,
    )


def _annotations(item: Any) -> Mapping[str, object] | None:
    annotations = getattr(item, "annotations", None)
    if annotations is None:
        return None
    return cast(Mapping[str, object], annotations.model_dump(by_alias=True, exclude_none=True))


def _content_block(item: Any) -> McpContentBlock:
    """Map one MCP content block, keeping every standard type."""
    if isinstance(item, types.TextContent):
        return McpContentBlock(block_type="text", text=item.text, annotations=_annotations(item))
    if isinstance(item, types.ImageContent):
        return McpContentBlock(
            block_type="image",
            data=item.data,
            mime_type=item.mimeType,
            annotations=_annotations(item),
        )
    if isinstance(item, types.AudioContent):
        return McpContentBlock(
            block_type="audio",
            data=item.data,
            mime_type=item.mimeType,
            annotations=_annotations(item),
        )
    if isinstance(item, types.ResourceLink):
        return McpContentBlock(
            block_type="resource_link",
            uri=str(item.uri),
            name=item.name,
            mime_type=item.mimeType,
            annotations=_annotations(item),
        )
    if isinstance(item, types.EmbeddedResource):
        return McpContentBlock(
            block_type="resource",
            resource=_resource_content(item.resource, _annotations(item)),
        )
    return McpContentBlock(block_type=type(item).__name__)


def _error_text(result: types.CallToolResult) -> str:
    text = "\n".join(item.text for item in result.content if isinstance(item, types.TextContent))
    return text or "The MCP server reported a tool error"
