"""Chat-driven setup: what the agent may call, and the steps only the owner can take.

The agent-facing half (``execute``) can look things up, open proposals and make access smaller.
It cannot publish, activate, link, store a secret or widen anything. The owner-facing half
(``lookup`` through ``reveal``) is reached only from the review pages, after the owner's code.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for

from ..application.control_plane_service import ControlPlaneService
from ..catalog.canonical import canonical_json, digest, freeze_json, thaw_json
from ..connections.service import ConnectionService, hash_token
from ..control_plane.compile_catalog import compile_catalog
from ..credentials.resolver import CredentialResolver
from ..discovery.client import DiscoveryClient
from ..domain.errors import (
    AuthorizationError,
    ConflictError,
    ControlPlaneError,
    EntityNotFoundError,
    InvalidStateError,
    RateLimitExceededError,
    RequestValidationError,
    UnsafeEndpointError,
)
from ..domain.models import (
    ConnectionRecord,
    DataPlaneRequest,
    DataPlaneResponse,
    DiscoverySnapshot,
    NormalizedContent,
    PublishedCatalog,
    PublishedPrimitive,
    ResolvedCredential,
    ServerRegistration,
)
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from ..policy import expiry
from ..policy.argument_limits import describe_limit, no_looser
from ..transport.security import EndpointSecurityPolicy
from . import catalog as setup_catalog
from . import directory, owner
from .compile import (
    Computed,
    Final,
    apply_limits,
    classify_change,
    compile_proposal,
    exposures_of,
    finalize,
    live_policies,
)
from .models import (
    ACTOR_AGENT,
    ACTOR_OWNER,
    ACTOR_SYSTEM,
    APPLIED,
    APPROVED,
    CONNECTING,
    DECLINED,
    EXPIRED,
    FAILED,
    IN_REVIEW,
    KIND_CHANGE,
    KIND_CONNECT,
    OPEN_STATES,
    SETUP_SERVER_ID,
    WAITING,
    SetupRequest,
    parse_limits,
)
from .store import Proposal, SetupStore

DiscoveryFactory = Callable[[ResolvedCredential | None], DiscoveryClient]
_PLANS = ("none", "env_token", "oauth", "oauth_app")
_MAX_SECRET = 4096
_CLIENT_ID = re.compile(r"[\x21-\x7e]{1,500}")  # printable, no spaces
_SHOWN = {APPLIED: "ready"}


class LoginLinks(Protocol):
    def login_link(self, connection_id: str, continue_to: str | None = None) -> str: ...


@dataclass(frozen=True, slots=True)
class SetupSettings:
    proposal_ttl_seconds: int = 24 * 3600
    session_ttl_seconds: int = 1800
    reveal_ttl_seconds: int = 3600
    max_open_per_connection: int = 5
    attempt_limit: int = 5
    attempt_window_seconds: int = 900


@dataclass(frozen=True, slots=True)
class ProposalInfo:
    """What the review pages need to say about where a proposal points."""

    name: str
    endpoint: str
    host: str
    listed: bool  # the address came from the adapter's own list of services
    plan: str  # none | env_token | oauth | oauth_app | choose (the owner picks, if unlisted)
    secret_label: str | None
    token_help: str | None
    docs: str | None = None  # the vendor's own page, for a listed service


@dataclass(frozen=True, slots=True)
class Revealed:
    token: str
    connection_code: str | None
    api_description_url: str
    label: str


class SetupService:
    def __init__(
        self,
        *,
        repository: SqliteControlPlaneRepository,
        store: SetupStore,
        control_plane: ControlPlaneService,
        connections: ConnectionService,
        credentials: CredentialResolver,
        discovery: DiscoveryFactory,
        login_links: LoginLinks | None,
        public_url: str,
        clock: Callable[[], str],
        endpoint_policy: EndpointSecurityPolicy,
        settings: SetupSettings | None = None,
    ) -> None:
        self._repository = repository
        self._store = store
        self._control = control_plane
        self._connections = connections
        self._credentials = credentials
        self._discovery = discovery
        self._login_links = login_links
        self._public_url = public_url.rstrip("/")
        self._clock = clock
        self._policy = endpoint_policy
        self._settings = settings or SetupSettings()
        self._version = setup_catalog.catalog_version()
        self._validators: dict[str, Validator] = {}
        for name, schema in setup_catalog.input_schemas().items():
            plain = cast(dict[str, object], thaw_json(schema))
            self._validators[name] = validator_for(plain)(plain)
        self._handlers: dict[str, Callable[[str, Mapping[str, object]], dict[str, object]]] = {
            "how_setup_works": self._how_setup_works,
            "find_server": self._find_server,
            "propose_connection": self._propose_connection,
            "propose_change": self._propose_change,
            "get_proposal_status": self._get_proposal_status,
            "list_connections": self._list_connections,
            "explain_connection": self._explain_connection,
            "turn_off_operations": self._turn_off_operations,
            "limit_arguments": self._limit_arguments,
            "expire_access": self._expire_access,
            "remove_connection": self._remove_connection,
        }

    # ==================================================================================
    # What the agent can call
    # ==================================================================================

    def execute(self, request: DataPlaneRequest) -> DataPlaneResponse:
        operation = request.public_operation_id
        handler = self._handlers.get(operation)
        if handler is None:
            raise EntityNotFoundError(f"Operation {operation} is not available")
        arguments = cast(Mapping[str, object], thaw_json(request.arguments))
        instance: Any = arguments
        try:
            self._validators[operation].validate(instance)
        except ValidationError as error:
            path = ".".join(str(item) for item in error.absolute_path) or "the request"
            raise RequestValidationError(f"{path}: {error.message}") from error
        self.sweep()
        body = handler(request.connection_id, arguments)
        return self._respond(request, body)

    def _respond(self, request: DataPlaneRequest, body: Mapping[str, object]) -> DataPlaneResponse:
        text = str(body.get("message", ""))
        step = body.get("nextStep")
        if isinstance(step, str):
            text = f"{text}\n\nNext: {step}".strip()
        provenance = freeze_json(
            {
                "serverId": SETUP_SERVER_ID,
                "catalogVersion": self._version,
                "primitiveKey": f"tool:{request.public_operation_id}",
                "mcpName": request.public_operation_id,
            }
        )
        return DataPlaneResponse(
            request_id=request.request_id,
            catalog_version=self._version,
            public_operation_id=request.public_operation_id,
            primitive_type="tool",
            provenance=cast(Mapping[str, object], provenance),
            operation_id=None,
            operation_status="COMPLETED",
            structured_content=cast(Mapping[str, object], freeze_json(body)),
            contents=(NormalizedContent("text", text, "text/plain", None, {}),),
        )

    def _how_setup_works(self, _: str, __: Mapping[str, object]) -> dict[str, object]:
        return {
            "message": "How to set up a connection for the user.",
            "steps": list(setup_catalog.STEPS),
            "rules": list(setup_catalog.RULES),
        }

    def _find_server(self, _: str, arguments: Mapping[str, object]) -> dict[str, object]:
        found = directory.search(str(arguments["query"]))
        if not found:
            return {
                "message": "No listed service matches.",
                "servers": [],
                "nextStep": (
                    "Ask the user for the https address of the service's MCP server and give it "
                    "to propose_connection as endpoint."
                ),
            }
        return {
            "message": f"{len(found)} listed service(s) match.",
            "servers": [
                {
                    "id": entry.id,
                    "name": entry.name,
                    "for": entry.summary,
                    "signIn": entry.sign_in_plan(),
                    "address": entry.endpoint,
                }
                for entry in found
            ],
            "nextStep": "Call propose_connection with the id of the service the user asked for.",
        }

    def _propose_connection(
        self, connection_id: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        request = SetupRequest.parse(arguments, change=False)
        entry = directory.get(request.server) if request.server else None
        if request.server and entry is None:
            raise RequestValidationError(
                f"{request.server} is not in the list. Use find_server, or give its https "
                "address as endpoint."
            )
        endpoint = entry.endpoint if entry else str(request.endpoint)
        self._check_endpoint(endpoint)
        server_id = entry.id if entry else self._derive_server_id(endpoint)
        drafts = self._drafts()
        taken = [c for c in self._active_connections(server_id) if c.connection_id not in drafts]
        if taken:
            raise ConflictError(
                f"{server_id} is already connected (connection {taken[0].connection_id}). "
                "Use propose_change to change what it may do."
            )
        return self._open(
            connection_id,
            KIND_CONNECT,
            request,
            server_id=server_id,
            connection=None,
            what=f"connect {entry.name if entry else _host(endpoint)}",
        )

    def _propose_change(
        self, connection_id: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        request = SetupRequest.parse(arguments, change=True)
        target = self._repository.get_connection(str(request.connection_id))
        if target.revoked_at is not None:
            raise EntityNotFoundError("That connection no longer exists")
        if target.server_id == SETUP_SERVER_ID:
            raise ConflictError("The setup connection cannot be changed from here")
        if not (
            request.read_only
            or request.also_allow
            or request.timeout_seconds
            or request.max_result_bytes
            or request.expires_in_hours
            or request.limits
        ):
            raise RequestValidationError(
                "There is nothing to add or change. To switch operations off use "
                "turn_off_operations, and to add limits on what they accept use limit_arguments."
            )
        return self._open(
            connection_id,
            KIND_CHANGE,
            request,
            server_id=target.server_id,
            connection=target,
            what=f"change {target.label}",
        )

    def _open(
        self,
        requester: str,
        kind: str,
        request: SetupRequest,
        *,
        server_id: str,
        connection: ConnectionRecord | None,
        what: str,
    ) -> dict[str, object]:
        stored = request.to_json()
        duplicate = self._store.find_open_duplicate(requester, kind, stored)
        token = secrets.token_urlsafe(32)
        state = WAITING
        if duplicate is not None:
            self._store.reissue_token(duplicate.proposal_id, hash_token(token), self._clock())
            proposal_id = duplicate.proposal_id
            expires = duplicate.expires_at
            state = duplicate.state
        else:
            if self._store.count_open(requester) >= self._settings.max_open_per_connection:
                raise RateLimitExceededError(
                    "There are already several proposals waiting for the user. Ask them to "
                    "finish or decline those first."
                )
            proposal_id = f"prop_{secrets.token_hex(6)}"
            expires = self._after(self._settings.proposal_ttl_seconds)
            self._store.create_proposal(
                proposal_id=proposal_id,
                requester_connection_id=requester,
                kind=kind,
                user_words=request.user_words,
                request=stored,
                token_hash=hash_token(token),
                now=self._clock(),
                expires_at=expires,
                target_server_id=server_id,
                target_connection_id=connection.connection_id if connection else None,
            )
            self._audit(
                ACTOR_AGENT,
                "setup.proposal.opened",
                server_id,
                {"proposalId": proposal_id, "kind": kind, "requestDigest": digest(stored)},
            )
        return {
            "proposalId": proposal_id,
            "state": state,
            "reviewLink": self.review_url(token),
            "expiresAt": expires,
            "message": (
                f"Nothing has been done yet. The user must open the review link to {what}."
            ),
            "nextStep": (
                "Give the user this link exactly as it is and ask them to open it on their own "
                "phone or computer, not in your browser. They will sign in or enter a token "
                "there, see what would become available, and approve or decline. Never ask for a "
                "token or password in the chat. When they say they are done, call "
                "get_proposal_status with this proposalId."
            ),
        }

    def _get_proposal_status(
        self, connection_id: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        proposal = self._store.get_proposal(str(arguments["proposalId"]))
        if proposal.requester_connection_id != connection_id:
            raise EntityNotFoundError("That proposal does not exist")
        state = _SHOWN.get(proposal.state, proposal.state)
        body: dict[str, object] = {"proposalId": proposal.proposal_id, "state": state}
        messages = {
            WAITING: "Waiting for the user to open the review link and enter their owner code.",
            CONNECTING: "The user has started: they are signing in or entering a token.",
            IN_REVIEW: "The user is reviewing what would become available.",
            APPROVED: "The user approved. It is being put in place.",
            DECLINED: "The user declined. Do not propose it again unless they ask.",
            EXPIRED: "The proposal expired before the user finished. Propose again if they "
            "still want it.",
            FAILED: "It could not be completed. The user was told why on the review page.",
        }
        if proposal.state in messages:
            body["message"] = messages[proposal.state]
        if proposal.state == APPLIED and proposal.kind == KIND_CHANGE:
            body["message"] = "The change is in place."
            body["connector"] = {
                "operations": self._operations_of(str(proposal.target_server_id)),
            }
            body["nextStep"] = (
                "Tell the user. The connector's address and token are unchanged. If Muse still "
                "lists the old operations, ask the user to refresh the connector in Muse."
            )
        elif proposal.state == APPLIED and proposal.target_connection_id:
            record = self._repository.get_connection(proposal.target_connection_id)
            url = f"{self._public_url}/c/{record.slug}/openapi.json"
            body["message"] = "The connection is ready."
            operations = self._operations_of(record.server_id)
            body["connector"] = {
                "name": record.label,
                "apiDescriptionUrl": url,
                "operations": operations,
            }
            ends = sorted(
                str(item["accessEndsAt"]) for item in operations if "accessEndsAt" in item
            )
            body["nextStep"] = (
                f'Create a connector called "{record.label}" from the API spec at {url}. The user '
                "will click Connect and enter the connector token that the review page showed "
                "them. Do not ask for it in the chat."
                + (f" The access ends at {ends[0]}: tell the user." if ends else "")
            )
        elif proposal.state in OPEN_STATES:
            body["nextStep"] = "Wait for the user. Do not claim it is connected."
        return body

    def _list_connections(self, connection_id: str, _: Mapping[str, object]) -> dict[str, object]:
        connections: list[dict[str, object]] = []
        drafts = self._drafts()
        for record in self._repository.list_connections():
            if (
                record.revoked_at is not None
                or record.server_id == SETUP_SERVER_ID
                or record.connection_id in drafts
            ):
                continue
            server = self._repository.get_server(record.server_id)
            connections.append(
                {
                    "connectionId": record.connection_id,
                    "label": record.label,
                    "serverId": record.server_id,
                    "enabled": server.enabled,
                    "operations": self._operations_of(record.server_id),
                }
            )
        waiting = [
            {"proposalId": item.proposal_id, "kind": item.kind, "state": item.state}
            for item in self._store.list_proposals(states=OPEN_STATES)
            if item.requester_connection_id == connection_id
        ]
        return {
            "message": f"{len(connections)} connection(s), {len(waiting)} proposal(s) waiting.",
            "connections": connections[:50],
            "proposalsWaiting": waiting[:50],
        }

    def _explain_connection(self, _: str, arguments: Mapping[str, object]) -> dict[str, object]:
        record = self._repository.get_connection(str(arguments["connectionId"]))
        if record.revoked_at is not None or record.server_id == SETUP_SERVER_ID:
            raise EntityNotFoundError("That connection does not exist")
        active = self._repository.get_active_catalog(record.server_id)
        origin = self._store.provenance(record.server_id, active.catalog_version)
        body: dict[str, object] = {
            "connectionId": record.connection_id,
            "catalogVersion": active.catalog_version,
            "operations": self._operations_of(record.server_id),
        }
        if origin is None or origin.get("proposalId") is None:
            body["message"] = (
                "This was set up at the command line or before chat setup existed, so there is "
                "no recorded request."
                if origin is None
                else f"The current exposure came from the adapter itself ({origin['origin']})."
            )
            return body
        proposal = self._store.get_proposal(str(origin["proposalId"]))
        decision = proposal.decision or {}
        body["message"] = "This is what the user asked for and approved."
        body["request"] = {
            "proposalId": proposal.proposal_id,
            "userWords": proposal.user_words,
            "approvedAt": decision.get("decidedAt"),
            "ownerAddedOperations": decision.get("addedByOwner", []),
            "ownerRemovedOperations": decision.get("removedByOwner", []),
        }
        return body

    def _turn_off_operations(self, _: str, arguments: Mapping[str, object]) -> dict[str, object]:
        record = self._connection_to_narrow(str(arguments["connectionId"]))
        names = [str(item) for item in cast(list[object], arguments["operations"])]
        old = self._repository.get_active_catalog(record.server_id)
        exposed = {item.public_operation_id for item in old.primitives}
        unknown = sorted(set(names) - exposed)
        if unknown:
            raise RequestValidationError(
                "This connection does not expose: "
                + ", ".join(unknown)
                + ". It exposes: "
                + (", ".join(sorted(exposed)) or "nothing")
                + "."
            )
        version = self._narrow(old, lambda item: item.public_operation_id not in set(names))
        self._audit(
            ACTOR_AGENT,
            "setup.catalog.narrowed",
            record.server_id,
            {"connectionId": record.connection_id, "removed": sorted(names), "version": version},
        )
        return {
            "message": f"Switched off: {', '.join(sorted(names))}.",
            "connectionId": record.connection_id,
            "operations": self._operations_of(record.server_id),
            "nextStep": "Tell the user. If Muse still lists the operations, they are gone from "
            "the adapter; ask the user to refresh the connector if needed.",
        }

    def _limit_arguments(self, _: str, arguments: Mapping[str, object]) -> dict[str, object]:
        record = self._connection_to_narrow(str(arguments["connectionId"]))
        limits = parse_limits(arguments["limitArguments"])
        old = self._repository.get_active_catalog(record.server_id)
        index = {
            item.primitive_key: cast(Mapping[str, object], thaw_json(item.definition))
            for item in old.primitives
        }
        before = {
            item.primitive_key: cast(dict[str, object], thaw_json(item.policy))
            for item in old.primitives
        }
        after = {key: dict(entry) for key, entry in before.items()}
        problems = apply_limits(after, index, limits)
        if problems:
            exposed = ", ".join(sorted(item.public_operation_id for item in old.primitives))
            raise RequestValidationError(
                "These limits cannot be applied: "
                + "; ".join(problems)
                + ". This connection exposes: "
                + (exposed or "nothing")
                + "."
            )
        looser = _loosened(before, after)
        if looser:
            raise ConflictError(
                looser + " To change a limit, use propose_change so the user can approve it."
            )
        if after == before:
            return {
                "message": "Those limits are already in place.",
                "connectionId": record.connection_id,
                "operations": self._operations_of(record.server_id),
            }
        version = self._narrow(old, lambda _item: True, edited=after, prefix="lim")
        self._audit(
            ACTOR_AGENT,
            "setup.catalog.limited",
            record.server_id,
            {
                "connectionId": record.connection_id,
                "limits": [limit.to_json() for limit in limits],
                "version": version,
            },
        )
        return {
            "message": "The limits are in place. Muse can now call those operations only with "
            "the values allowed.",
            "connectionId": record.connection_id,
            "operations": self._operations_of(record.server_id),
            "nextStep": "Tell the user. If Muse still shows the old inputs, ask the user to "
            "refresh the connector.",
        }

    def _expire_access(self, _: str, arguments: Mapping[str, object]) -> dict[str, object]:
        record = self._connection_to_narrow(str(arguments["connectionId"]))
        hours = cast(int, arguments["hours"])
        old = self._repository.get_active_catalog(record.server_id)
        end = self._after(hours * 3600)
        edited: dict[str, dict[str, object]] = {}
        for item in old.primitives:
            policy = cast(dict[str, object], thaw_json(item.policy))
            # shorten it, or give it an end; never move an earlier end to a later time
            if expiry.no_later(policy.get("expiresAt"), end) and policy.get("expiresAt") != end:
                policy["expiresAt"] = end
                edited[item.primitive_key] = policy
        if not edited:
            return {
                "message": "Access already ends sooner than that.",
                "connectionId": record.connection_id,
                "operations": self._operations_of(record.server_id),
            }
        version = self._narrow(old, lambda _item: True, edited=edited, prefix="exp")
        self._audit(
            ACTOR_AGENT,
            "setup.catalog.expiry",
            record.server_id,
            {
                "connectionId": record.connection_id,
                "expiresAt": end,
                "hours": hours,
                "version": version,
            },
        )
        return {
            "message": f"Access to {record.label} now ends at {end}.",
            "connectionId": record.connection_id,
            "operations": self._operations_of(record.server_id),
            "nextStep": "Tell the user when it ends. After that Muse can no longer use it; to "
            "extend it, use propose_change with expiresInHours.",
        }

    def _remove_connection(self, _: str, arguments: Mapping[str, object]) -> dict[str, object]:
        record = self._connection_to_narrow(str(arguments["connectionId"]))
        self._connections.unlink(record.connection_id, actor=ACTOR_AGENT)
        self._forget_secret_of(record)
        if not self._active_connections(record.server_id):
            self._control.set_server_enabled(record.server_id, enabled=False, actor=ACTOR_AGENT)
        self._audit(
            ACTOR_AGENT,
            "setup.connection.removed",
            record.server_id,
            {"connectionId": record.connection_id},
        )
        return {
            "message": f"{record.label} is disconnected. Muse no longer has access to it.",
            "nextStep": "Tell the user. They can ask to connect it again at any time.",
        }

    # ==================================================================================
    # What only the owner can do (called from the review pages)
    # ==================================================================================

    def review_url(self, token: str) -> str:
        return f"{self._public_url}/review/{token}"

    def sweep(self) -> None:
        """Expire what is overdue and forget what is no longer needed."""
        now = self._clock()
        for proposal in self._store.expire_due(now):
            self._cleanup_draft(proposal)
            self._audit(
                ACTOR_SYSTEM,
                "setup.proposal.expired",
                proposal.target_server_id,
                {"proposalId": proposal.proposal_id},
            )
        self._store.wipe_old_reveals(self._before(self._settings.reveal_ttl_seconds))
        self._store.drop_sessions(now)

    def lookup(self, token: str) -> Proposal | None:
        self.sweep()
        return self._store.find_by_token(hash_token(token))

    def locked(self, proposal: Proposal) -> bool:
        since = self._before(self._settings.attempt_window_seconds)
        scope = f"proposal:{proposal.proposal_id}"
        return self._store.count_attempts(scope, since) >= self._settings.attempt_limit

    def unlock(self, proposal: Proposal, code: str) -> str | None:
        """Check the owner's code. Returns a session secret, or None for a wrong code."""
        if self.locked(proposal):
            raise AuthorizationError("Too many wrong codes. This link is locked for a while.")
        stored = self._store.owner_code()
        if stored is None:
            raise InvalidStateError("No owner code has been set. Run chat-setup-enable.")
        scope = f"proposal:{proposal.proposal_id}"
        if not owner.matches(code, stored[1], stored[0]):
            self._store.add_attempt(scope, self._clock())
            self._audit(
                ACTOR_OWNER,
                "setup.unlock.failed",
                proposal.target_server_id,
                {"proposalId": proposal.proposal_id},
            )
            return None
        self._store.clear_attempts(scope)
        secret = secrets.token_urlsafe(24)
        self._store.add_session(
            _hash(secret), self._clock(), self._after(self._settings.session_ttl_seconds)
        )
        return secret

    def session_valid(self, secret: str | None) -> bool:
        return bool(secret) and self._store.session_valid(_hash(secret), self._clock())

    def describe(self, proposal: Proposal) -> ProposalInfo:
        if proposal.kind == KIND_CHANGE:
            server = self._repository.get_server(str(proposal.target_server_id))
            existing = self._repository.get_connection(str(proposal.target_connection_id))
            return ProposalInfo(
                server.display_name,
                server.endpoint,
                _host(server.endpoint),
                True,
                existing.credential_kind,
                None,
                None,
            )
        request = SetupRequest.parse(proposal.request, change=False)
        entry = directory.get(request.server) if request.server else None
        if entry is not None:
            return ProposalInfo(
                entry.name,
                entry.endpoint,
                _host(entry.endpoint),
                True,
                entry.credential,
                entry.secret_label,
                entry.token_help,
                entry.docs,
            )
        endpoint = str(request.endpoint)
        return ProposalInfo(_host(endpoint), endpoint, _host(endpoint), False, "choose", None, None)

    def start(
        self,
        proposal: Proposal,
        *,
        plan: str | None,
        secret: str | None,
        return_path: str,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> str | None:
        """Begin the connection. Returns where to send the browser to sign in, or None."""
        if proposal.state not in (WAITING, CONNECTING):
            raise ConflictError("This proposal is not waiting to be connected")
        signing_in = True  # pressing the button again on the resume page means sign in again
        if proposal.state == WAITING:
            signing_in = proposal.kind == KIND_CONNECT
            if proposal.kind == KIND_CONNECT:
                proposal = self._create_draft(proposal, plan, secret, client_id, client_secret)
            else:
                proposal = self._store.transition(
                    proposal.proposal_id, expected=(WAITING,), state=CONNECTING, now=self._clock()
                )
        if not signing_in or proposal.target_connection_id is None:
            return None
        record = self._repository.get_connection(proposal.target_connection_id)
        if record.credential_kind != "oauth":
            return None
        if self._login_links is None:
            raise InvalidStateError("Signing in to a service is not available on this deployment")
        return self._login_links.login_link(record.connection_id, continue_to=return_path)

    def cancel(self, proposal: Proposal) -> Proposal:
        """The owner changes their mind before there is anything to review."""
        if proposal.state not in (WAITING, CONNECTING):
            raise ConflictError("This proposal is not waiting to be connected")
        return self._decline(proposal)

    def discover(self, proposal: Proposal) -> Proposal:
        """Look at what the server offers now, with the connection's own credential, and compile."""
        if proposal.state != CONNECTING:
            raise ConflictError("This proposal is not ready to look at the server")
        server_id = str(proposal.target_server_id)
        record = self._repository.get_connection(str(proposal.target_connection_id))
        credential = self._credentials.resolve(
            local_identity_id=record.identity_id,
            connection_id=record.connection_id,
            server_id=server_id,
            required_scopes=frozenset(),
        )
        snapshot = self._control.discover(server_id, self._discovery(credential), actor=ACTOR_OWNER)
        request = SetupRequest.parse(proposal.request, change=proposal.kind == KIND_CHANGE)
        current: dict[str, dict[str, object]] | None = None
        digests: dict[str, str] | None = None
        base: str | None = None
        if proposal.kind == KIND_CHANGE:
            active = self._repository.get_active_catalog(server_id)
            current, digests = live_policies(active)
            base = active.catalog_version
        computed = compile_proposal(
            primitives=snapshot.primitives,
            request=request,
            current=current,
            current_digests=digests,
            snapshot_digest=snapshot.snapshot_digest,
            base_version=base,
        )
        if computed.inherited_expiry is not None and expiry.has_ended(
            {"expiresAt": computed.inherited_expiry}, self._clock()
        ):
            computed = replace(computed, inherited_ended=True)  # only changes what is shown
        updated = self._store.transition(
            proposal.proposal_id,
            expected=(CONNECTING,),
            state=IN_REVIEW,
            now=self._clock(),
            snapshot_id=snapshot.snapshot_id,
            computed=computed.to_json(),
            digest=computed.digest,
        )
        self._audit(
            ACTOR_OWNER,
            "setup.proposal.compiled",
            server_id,
            {
                "proposalId": proposal.proposal_id,
                "snapshotDigest": snapshot.snapshot_digest,
                "proposalDigest": computed.digest,
                "offered": len(computed.candidates),
                "proposed": len(computed.selected),
            },
        )
        return updated

    def decide(
        self,
        proposal: Proposal,
        *,
        approve: bool,
        shown_digest: str,
        chosen: Collection[str],
        reviewed_read: Collection[str],
    ) -> Proposal:
        if proposal.state != IN_REVIEW:
            raise ConflictError("This proposal is not waiting for a decision")
        if not hmac.compare_digest(shown_digest, proposal.digest or ""):
            raise ConflictError("What you were shown has changed. Reload the page and look again.")
        if not approve:
            return self._decline(proposal)
        computed = Computed.from_json(cast(Mapping[str, object], proposal.computed))
        hours = computed.expires_in_hours
        expires_at = self._after(hours * 3600) if hours else None  # counted from now, the approval
        final = finalize(computed, chosen, reviewed_read, expires_at)
        decision: dict[str, object] = {
            "decision": "approved",
            "method": "owner-code",
            "decidedAt": self._clock(),
            "proposalDigest": proposal.digest,
            "finalDigest": final.digest,
            "chosen": list(final.selected),
            "markedRead": list(final.marked_read),
            "addedByOwner": list(final.added_by_owner),
            "removedByOwner": list(final.removed_by_owner),
            "exposedNotRequested": list(final.exposed_not_requested),
            "expiresAt": expires_at or computed.inherited_expiry,
        }
        approved = self._store.transition(
            proposal.proposal_id,
            expected=(IN_REVIEW,),
            state=APPROVED,
            now=self._clock(),
            decision=decision,
        )
        self._audit(
            ACTOR_OWNER,
            "setup.proposal.approved",
            approved.target_server_id,
            {
                "proposalId": approved.proposal_id,
                "proposalDigest": proposal.digest,
                "finalDigest": final.digest,
                "exposed": list(final.selected),
            },
        )
        return self._apply(approved, final)

    def reveal(self, proposal: Proposal) -> Revealed | None:
        """The connector address and token, for the owner to copy into Muse."""
        if proposal.state != APPLIED:
            return None
        text = self._store.reveal_text(proposal.proposal_id)
        if text is None:
            return None
        data = cast(Mapping[str, object], json.loads(text))
        code = data.get("connectionCode")
        return Revealed(
            token=str(data["token"]),
            connection_code=code if isinstance(code, str) else None,
            api_description_url=f"{self._public_url}/c/{data['slug']}/openapi.json",
            label=str(data["label"]),
        )

    def collected(self, proposal: Proposal) -> None:
        """The owner has copied the token: forget it."""
        if proposal.state == APPLIED:
            self._store.transition(
                proposal.proposal_id,
                expected=(APPLIED,),
                state=APPLIED,
                now=self._clock(),
                reveal=None,
            )

    def reissue_link(self, proposal_id: str) -> str:
        """A fresh review link for the owner at the command line; the old one stops working."""
        token = secrets.token_urlsafe(32)
        self._store.reissue_token(proposal_id, hash_token(token), self._clock())
        return self.review_url(token)

    # --- owner steps, in detail -----------------------------------------------------------

    def _create_draft(
        self,
        proposal: Proposal,
        plan: str | None,
        secret: str | None,
        client_id: str | None,
        client_secret: str | None,
    ) -> Proposal:
        request = SetupRequest.parse(proposal.request, change=False)
        info = self.describe(proposal)
        chosen = info.plan if info.plan != "choose" else (plan or "none")
        if chosen not in _PLANS:
            raise RequestValidationError(
                "Choose how to sign in: no sign-in, a token, or OAuth (with an app if needed)"
            )
        server_id = str(proposal.target_server_id)
        secret_name: str | None = None
        app: tuple[str, str | None] | None = None
        if chosen == "oauth_app":  # check what was typed before anything is created
            app = (_clean_client_id(client_id), _clean_optional_secret(client_secret))
        if chosen == "env_token":
            secret_name = self._secret_name(proposal.proposal_id)
            self._store.put_secret(secret_name, _clean_secret(secret), self._clock())
        kind = "oauth" if chosen == "oauth_app" else chosen  # the app only changes how it signs in
        try:
            existing = self._existing_server(server_id)
            if existing is None:
                self._control.register_server(
                    server_id=server_id,
                    display_name=info.name,
                    endpoint=info.endpoint,
                    auth_profile=kind,
                    actor=ACTOR_OWNER,
                )
            else:
                if existing.endpoint != info.endpoint:
                    raise ConflictError(f"{server_id} is already registered at another address")
                if self._active_connections(server_id):
                    raise ConflictError(f"{server_id} is already connected")
                if not existing.enabled:
                    self._control.set_server_enabled(server_id, enabled=True, actor=ACTOR_OWNER)
            linked = self._connections.link(
                server_id=server_id,
                label=info.name,
                credential_kind=kind,
                credential_ref=secret_name or "",
                actor=ACTOR_OWNER,
            )
        except Exception:
            if secret_name is not None:
                self._store.delete_secret(secret_name)
            raise
        if app is not None:
            try:
                self._store.put_oauth_client(
                    linked.record.connection_id, app[0], app[1], self._clock()
                )
            except Exception:
                self._undo_draft(server_id, linked.record.connection_id)
                raise
        sealed = canonical_json(
            {
                "token": linked.token,
                "connectionCode": linked.link_code,
                "slug": linked.record.slug,
                "label": linked.record.label,
            }
        )
        updated = self._store.transition(
            proposal.proposal_id,
            expected=(WAITING,),
            state=CONNECTING,
            now=self._clock(),
            target_connection_id=linked.record.connection_id,
            reveal=sealed,
        )
        self._audit(
            ACTOR_OWNER,
            "setup.proposal.connecting",
            server_id,
            {
                "proposalId": proposal.proposal_id,
                "connectionId": linked.record.connection_id,
                "signIn": chosen,
                "ownApp": app is not None,
                "listed": info.listed,
                "requestDigest": digest(request.to_json()),
            },
        )
        return updated

    def _decline(self, proposal: Proposal) -> Proposal:
        declined = self._store.transition(
            proposal.proposal_id,
            expected=OPEN_STATES,
            state=DECLINED,
            now=self._clock(),
            decision={
                "decision": "declined",
                "method": "owner-code",
                "decidedAt": self._clock(),
                "proposalDigest": proposal.digest,
            },
            reveal=None,
        )
        self._cleanup_draft(declined)
        self._audit(
            ACTOR_OWNER,
            "setup.proposal.declined",
            declined.target_server_id,
            {"proposalId": declined.proposal_id, "proposalDigest": proposal.digest},
        )
        return declined

    def _apply(self, proposal: Proposal, final: Final) -> Proposal:
        """Publish and activate what the owner approved. Safe to repeat after a crash."""
        server_id = str(proposal.target_server_id)
        version = f"chat-{proposal.proposal_id.removeprefix('prop_')}"
        try:
            computed = Computed.from_json(cast(Mapping[str, object], proposal.computed))
            if proposal.kind == KIND_CHANGE:
                active = self._repository.get_active_catalog(server_id).catalog_version
                if active not in (computed.base_version, version):
                    raise ConflictError(
                        "What is live changed while you were reviewing. Ask for the change again."
                    )
            snapshot = self._repository.get_discovery(int(cast(int, proposal.snapshot_id)))
            published = self._publish(server_id, snapshot, version, final.policies)
            self._activate(server_id, version)
        except Exception as error:
            self._fail(proposal, str(error))
            raise
        self._store.record_provenance(
            server_id=server_id,
            catalog_version=version,
            origin="proposal",
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.digest,
            now=self._clock(),
        )
        applied = self._store.transition(
            proposal.proposal_id,
            expected=(APPROVED,),
            state=APPLIED,
            now=self._clock(),
            applied={
                "catalogVersion": version,
                "catalogDigest": published.catalog_digest,
                "activatedAt": self._clock(),
            },
        )
        self._audit(
            ACTOR_OWNER,
            "setup.proposal.applied",
            server_id,
            {
                "proposalId": proposal.proposal_id,
                "catalogVersion": version,
                "catalogDigest": published.catalog_digest,
                "finalDigest": final.digest,
            },
        )
        return applied

    def _publish(
        self,
        server_id: str,
        discovered: DiscoverySnapshot,
        version: str,
        policies: tuple[Mapping[str, object], ...],
    ) -> PublishedCatalog:
        server = self._repository.get_server(server_id)
        definition: dict[str, object] = {
            "id": server.server_id,
            "endpoint": server.endpoint,
            "authProfile": server.auth_profile,
        }
        catalog = compile_catalog(
            catalog_version=version,
            server=definition,
            protocol_version=discovered.protocol_version,
            discovered_at=discovered.discovered_at,
            published_at=self._clock(),
            primitives=discovered.primitives,
            policies=policies,
        )
        try:
            saved = self._repository.save_catalog(catalog)
        except ConflictError:
            saved = self._repository.get_catalog(server_id, version)
        self._audit(
            ACTOR_OWNER,
            "catalog.publish",
            server_id,
            {"version": version, "digest": saved.catalog_digest, "via": "chat-setup"},
        )
        return saved

    def _activate(self, server_id: str, version: str) -> None:
        try:
            if self._repository.get_active_catalog(server_id).catalog_version == version:
                return
        except EntityNotFoundError:
            pass
        self._control.activate_catalog(server_id, version, actor=ACTOR_OWNER)

    def _fail(self, proposal: Proposal, reason: str) -> None:
        try:
            failed = self._store.transition(
                proposal.proposal_id,
                expected=(APPROVED, IN_REVIEW, CONNECTING),
                state=FAILED,
                now=self._clock(),
                error=reason[:300],
                reveal=None,
            )
        except ConflictError:
            return
        self._cleanup_draft(failed)
        self._audit(
            ACTOR_SYSTEM,
            "setup.proposal.failed",
            failed.target_server_id,
            {"proposalId": failed.proposal_id, "reason": reason[:300]},
        )

    # ==================================================================================
    # Helpers
    # ==================================================================================

    def _narrow(
        self,
        old: PublishedCatalog,
        keep: Callable[[PublishedPrimitive], bool],
        edited: Mapping[str, Mapping[str, object]] | None = None,
        *,
        prefix: str = "off",
    ) -> str:
        """Publish the live catalog without some operations, or with tighter policies for some,
        reusing its own definitions. Refuses anything that would give Muse more."""
        kept = [item for item in old.primitives if keep(item)]
        changed = edited or {}
        policies = [
            dict(changed[item.primitive_key])
            if item.primitive_key in changed
            else cast(dict[str, object], thaw_json(item.policy))
            for item in kept
        ]
        primitives = [cast(dict[str, object], thaw_json(item.definition)) for item in kept]
        stamp = self._clock().replace(":", "").replace("-", "")[:15]
        version = f"{prefix}-{stamp}-{secrets.token_hex(3)}"
        new = compile_catalog(
            catalog_version=version,
            server=cast(Mapping[str, object], thaw_json(old.server)),
            protocol_version=old.protocol_version,
            discovered_at=old.discovered_at,
            published_at=self._clock(),
            primitives=primitives,
            policies=policies,
        )
        if classify_change(exposures_of(old), exposures_of(new)) not in ("narrowing", "none"):
            raise ConflictError("That change would give Muse more, so it needs the owner")
        server_id = str(old.server["id"])
        self._repository.save_catalog(new)
        self._control.activate_catalog(server_id, version, actor=ACTOR_AGENT)
        self._store.record_provenance(
            server_id=server_id,
            catalog_version=version,
            origin="narrowing",
            proposal_id=None,
            proposal_digest=None,
            now=self._clock(),
        )
        return version

    def _connection_to_narrow(self, connection_id: str) -> ConnectionRecord:
        record = self._repository.get_connection(connection_id)
        if record.revoked_at is not None:
            raise EntityNotFoundError("That connection no longer exists")
        if record.server_id == SETUP_SERVER_ID:
            raise ConflictError("The setup connection cannot be changed from here")
        return record

    def operations(self, server_id: str) -> list[dict[str, object]]:
        """What a server currently exposes, as ids and kinds."""
        return self._operations_of(server_id)

    def _operations_of(self, server_id: str) -> list[dict[str, object]]:
        try:
            catalog = self._repository.get_active_catalog(server_id)
        except EntityNotFoundError:
            return []
        now = self._clock()
        return [
            {
                "id": item.public_operation_id,
                "kind": item.classification,
                **_limits_in_words(item),
                **_expiry_in_words(item, now),
            }
            for item in catalog.primitives
        ]

    def _drafts(self) -> set[str | None]:
        """Connections that only exist as the start of a proposal nobody has approved yet."""
        return {
            item.target_connection_id
            for item in self._store.list_proposals(states=(*OPEN_STATES, APPROVED))
            if item.kind == KIND_CONNECT
        }

    def _active_connections(self, server_id: str) -> list[ConnectionRecord]:
        return [
            item for item in self._repository.list_connections(server_id) if item.revoked_at is None
        ]

    def _existing_server(self, server_id: str) -> ServerRegistration | None:
        try:
            return self._repository.get_server(server_id)
        except EntityNotFoundError:
            return None

    def _check_endpoint(self, endpoint: str) -> None:
        try:
            self._policy.validate(endpoint, resolve=False)
        except UnsafeEndpointError as error:
            raise RequestValidationError(str(error)) from error

    def _derive_server_id(self, endpoint: str) -> str:
        host = urlsplit(endpoint).hostname or "server"
        slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
        if not slug or not slug[0].isalpha():
            slug = f"s-{slug}".strip("-")
        slug = slug[:56].strip("-")
        if len(slug) < 3:
            slug = f"{slug}-mcp"
        existing = self._existing_server(slug)
        if existing is None or existing.endpoint == endpoint:
            return slug
        return f"{slug}-{hashlib.sha256(endpoint.encode()).hexdigest()[:6]}"

    @staticmethod
    def _secret_name(proposal_id: str) -> str:
        return f"CHAT_SETUP_{proposal_id.upper()}"

    def _forget_secret_of(self, record: ConnectionRecord) -> None:
        if record.credential_kind == "env_token" and record.credential_ref.startswith(
            "CHAT_SETUP_"
        ):
            self._store.delete_secret(record.credential_ref)
        self._store.delete_oauth_client(record.connection_id)

    def _cleanup_draft(self, proposal: Proposal) -> None:
        """Undo what starting a connection created, when it is declined, expires or fails."""
        if proposal.kind != KIND_CONNECT:
            return
        self._store.delete_secret(self._secret_name(proposal.proposal_id))
        self._undo_draft(proposal.target_server_id, proposal.target_connection_id)

    def _undo_draft(self, server_id: str | None, connection_id: str | None) -> None:
        if connection_id is None:
            return
        with contextlib.suppress(ControlPlaneError):
            self._connections.unlink(connection_id, actor=ACTOR_SYSTEM)
        self._store.delete_oauth_client(connection_id)
        if server_id and not self._active_connections(server_id):
            with contextlib.suppress(ControlPlaneError):
                self._control.set_server_enabled(server_id, enabled=False, actor=ACTOR_SYSTEM)

    def _audit(
        self, actor: str, action: str, server_id: str | None, data: Mapping[str, object]
    ) -> None:
        self._repository.record_admin_audit(
            occurred_at=self._clock(), actor=actor, action=action, server_id=server_id, data=data
        )

    def _after(self, seconds: int) -> str:
        return _format(_parse(self._clock()) + timedelta(seconds=seconds))

    def _before(self, seconds: int) -> str:
        return _format(_parse(self._clock()) - timedelta(seconds=seconds))


def _host(endpoint: str) -> str:
    return urlsplit(endpoint).hostname or endpoint


def _expiry_in_words(item: PublishedPrimitive, now: str) -> dict[str, object]:
    ends = expiry.ends_at(item.policy)
    if ends is None:
        return {}
    return {"accessEndsAt": ends, "accessEnded": expiry.has_ended(item.policy, now)}


def _limits_in_words(item: PublishedPrimitive) -> dict[str, object]:
    raw = item.policy.get("argumentPatterns")
    if not isinstance(raw, Mapping) or not raw:
        return {}
    return {
        "limits": {
            str(name): describe_limit(str(pattern))
            for name, pattern in sorted(
                cast(Mapping[object, object], raw).items(), key=lambda pair: str(pair[0])
            )
        }
    }


def _loosened(
    before: Mapping[str, Mapping[str, object]], after: Mapping[str, Mapping[str, object]]
) -> str | None:
    """The first existing limit that a new one would replace with something wider."""
    for key, entry in before.items():
        old = entry.get("argumentPatterns")
        if not isinstance(old, Mapping):
            continue
        new = after[key].get("argumentPatterns")
        kept: Mapping[object, object] = (
            cast(Mapping[object, object], new) if isinstance(new, Mapping) else {}
        )
        for argument, pattern in cast(Mapping[object, object], old).items():
            replacement = kept.get(argument)
            if not isinstance(replacement, str) or not no_looser(str(pattern), replacement):
                return (
                    f"{entry.get('publicOperationId')} already limits {argument} to "
                    f"{describe_limit(str(pattern))}, so this would not narrow it."
                )
    return None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _clean_client_id(value: str | None) -> str:
    cleaned = (value or "").strip()
    if not _CLIENT_ID.fullmatch(cleaned):
        raise RequestValidationError(
            "Enter the app's client ID exactly as the service shows it (no spaces)"
        )
    return cleaned


def _clean_optional_secret(value: str | None) -> str | None:
    """The app's client secret. Some apps have none (a public client), so it may be empty."""
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_SECRET or any(ord(character) < 32 for character in cleaned):
        raise RequestValidationError("That does not look like a client secret")
    return cleaned


def _clean_secret(value: str | None) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise RequestValidationError("Enter the token for this service")
    if len(cleaned) > _MAX_SECRET or any(ord(character) < 32 for character in cleaned):
        raise RequestValidationError("That does not look like a token")
    return cleaned
