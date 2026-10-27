# pyright: basic
"""The two ways an agent can reach the personal-data server.

* ``DirectPath`` (A): the server connected straight to the agent, as a raw custom connector is.
* ``AdapterPath`` (B): the same server behind the adapter, reached through the connector face Muse
  uses (an OpenAPI document and a bearer token), with the owner's exposure policy applied.

Both give an agent the same two calls, ``list_tools`` and ``call``, and answer with the same
``ToolResult``, so a scenario script cannot tell which path it is on and every difference in the
outcome comes from the path.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable, Collection, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.application.policy_template import draft_policies
from muse_mcp_adapter.application.serve import ServeConfig, build_application
from muse_mcp_adapter.connections import ConnectionService
from muse_mcp_adapter.persistence.owner_store import OwnerStore
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.transport.sdk_client import McpSdkClient, StreamableHttpSessionFactory
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy

from .hosting import free_port, serve
from .mcp_direct import McpDirect

LOCAL = EndpointSecurityPolicy.local_development()
SERVER_ID = "household"
RESERVED = frozenset({"get_operation_status", "resume_operation"})


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    writes: bool


@dataclass
class ToolResult:
    """One answer, whichever path it came from."""

    ok: bool
    structured: Any
    text: str  # the plain text the server or adapter returned
    error: str | None  # an error code or message when not ok
    status: int | None  # the HTTP status on path B; None on path A
    latency_ms: float
    replayed: bool = False
    operation_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def context(self) -> str:
        """What this result puts into the agent's context."""
        if self.structured is not None:
            return json.dumps(self.structured, sort_keys=True)
        return self.text


class ToolPath(Protocol):
    name: str

    def list_tools(self) -> list[ToolInfo]: ...

    def call(
        self, tool: str, arguments: dict[str, Any], *, key: str | None = None
    ) -> ToolResult: ...


class DirectPath:
    name = "direct"

    def __init__(self, url: str) -> None:
        self._client = McpDirect(url)

    def list_tools(self) -> list[ToolInfo]:
        return [
            ToolInfo(
                tool["name"],
                tool["description"],
                tool["annotations"].get("readOnlyHint") is not True,
            )
            for tool in self._client.list_tools()
        ]

    def call(self, tool: str, arguments: dict[str, Any], *, key: str | None = None) -> ToolResult:
        # A bare MCP call has nowhere to carry an idempotency key, so ``key`` is ignored.
        started = time.perf_counter()
        answer = self._client.call(tool, arguments)
        elapsed = (time.perf_counter() - started) * 1000
        failed = bool(answer["isError"])
        return ToolResult(
            ok=not failed,
            structured=None if failed else answer["structured"],
            text=answer["text"],
            error=answer["text"] if failed else None,
            status=None,
            latency_ms=elapsed,
        )


class AdapterPath:
    name = "adapter"

    def __init__(
        self,
        base_url: str,
        token: str,
        operations: Mapping[str, Mapping[str, Any]],
        *,
        origin: str = "",
    ):
        self.origin = origin
        self.owner_log: list[dict[str, Any]] = []
        self._http = httpx.Client(base_url=base_url, timeout=60)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._operations = dict(operations)

    def list_tools(self) -> list[ToolInfo]:
        return [
            ToolInfo(
                name,
                str(item.get("description") or item.get("summary") or ""),
                bool(item.get("x-requires-confirmation")),
            )
            for name, item in sorted(self._operations.items())
        ]

    def call(self, tool: str, arguments: dict[str, Any], *, key: str | None = None) -> ToolResult:
        headers = dict(self._headers)
        if key is not None:
            headers["Idempotency-Key"] = key
        started = time.perf_counter()
        response = self._http.post(f"/operations/{tool}", json=arguments, headers=headers)
        elapsed = (time.perf_counter() - started) * 1000
        body: dict[str, Any] = response.json() if response.content else {}
        if response.status_code != 200:
            return ToolResult(
                ok=False,
                structured=None,
                text=str(body.get("message", "")),
                error=str(body.get("error", response.status_code)),
                status=response.status_code,
                latency_ms=elapsed,
                operation_id=body.get("operationId"),
                raw=body,
            )
        text = "\n".join(
            str(block.get("text", ""))
            for block in body.get("contents", [])
            if block.get("type") == "text"
        )
        return ToolResult(
            ok=True,
            structured=body.get("structuredContent"),
            text=text,
            error=None,
            status=200,
            latency_ms=elapsed,
            replayed=bool(body.get("idempotentReplay")),
            operation_id=body.get("operationId"),
            raw=body,
        )

    def execution(self, operation_id: str) -> dict[str, Any]:
        """The adapter's own record of one call: its status and events."""
        response = self._http.get(f"/executions/{operation_id}", headers=self._headers)
        return response.json()

    def close(self) -> None:
        self._http.close()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextmanager
def adapter_in_front_of(
    server_url: str,
    *,
    expose: Collection[str],
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
    owner_passcode: str | None = None,
    clock: Callable[[], str] | None = None,
    directory: Path | None = None,
) -> Generator[AdapterPath]:
    """Stand the adapter up in front of an MCP server and yield the connector path to it.

    The owner's choice is ``expose``: the tool names that are switched on. Everything else the
    server offers stays unpublished. Classification (read, write, sensitive write) is what the
    adapter drafts on its own from the server's hints and tool names. ``overrides`` adds policy
    fields per tool (for example ``ownerControls``). With ``owner_passcode`` the adapter runs in
    owner-approval mode: writes wait for the owner on the approval page.
    """
    with tempfile.TemporaryDirectory(prefix="trustlab-") as scratch:
        database = (directory or Path(scratch)) / "adapter.db"
        with SqliteControlPlaneRepository(database) as repository:
            repository.initialize()
            control = ControlPlaneService(repository, endpoint_policy=LOCAL)
            control.register_server(
                server_id=SERVER_ID,
                display_name="Household",
                endpoint=server_url,
                auth_profile="none",
            )
            snapshot = control.discover(
                SERVER_ID, McpSdkClient(StreamableHttpSessionFactory(endpoint_policy=LOCAL))
            )
            policies = draft_policies(snapshot, expose_reads=False)
            wanted = set(expose)
            known = {str(item["publicOperationId"]) for item in policies}
            unknown = wanted - known
            if unknown:
                raise ValueError(f"Cannot expose tools the server does not have: {sorted(unknown)}")
            extra = overrides or {}
            published = [
                {**item, **extra.get(str(item["publicOperationId"]), {}), "allowed": True}
                for item in policies
                if item["publicOperationId"] in wanted
            ]
            control.publish_catalog(
                server_id=SERVER_ID, catalog_version="1.0.0", policies=published
            )
            control.activate_catalog(SERVER_ID, "1.0.0")

            link = ConnectionService(repository).link(
                server_id=SERVER_ID, label="Agent", credential_kind="none", credential_ref=""
            )
            port = free_port()
            config = ServeConfig(
                public_url=f"http://127.0.0.1:{port}",
                allow_insecure_http=True,
                allow_private_networks=True,
                rate_limit=100_000,
            )
            if owner_passcode is not None:
                config = replace(config, approval_mode="owner")
                OwnerStore(repository).set_passcode(owner_passcode, _now())
            app = build_application(repository, config, {}, database=database, clock=clock)
            with serve(app, port):
                base = f"{config.public_url}/c/{link.record.slug}"
                spec = httpx.get(f"{base}/openapi.json", timeout=30).json()
                operations = {
                    path.rsplit("/", 1)[-1]: item["post"]
                    for path, item in spec["paths"].items()
                    if path.startswith("/operations/")
                }
                path = AdapterPath(base, link.token, operations, origin=config.public_url)
                try:
                    yield path
                finally:
                    path.close()
