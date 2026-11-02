"""Commands for chat-driven setup, run on the adapter's machine (never over the network)."""

from __future__ import annotations

import argparse
import hashlib
import secrets
from datetime import UTC, datetime

from ..application.control_plane_service import ControlPlaneService
from ..application.serve import ServeConfig, open_secret_box
from ..connections import ConnectionService
from ..domain.errors import EntityNotFoundError, InvalidStateError
from ..domain.models import ConnectionRecord
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from ..transport.security import EndpointSecurityPolicy
from . import owner
from .catalog import ensure_setup_server
from .models import SETUP_LABEL, SETUP_SERVER_ID
from .store import Proposal, SetupStore

COMMANDS = (
    "chat-setup-enable",
    "proposals",
    "proposal-link",
    "audit-verify",
    "explain-call",
    "explain-change",
)


_Subparsers = argparse._SubParsersAction  # pyright: ignore[reportPrivateUsage]


def add_parsers(commands: _Subparsers[argparse.ArgumentParser]) -> None:
    enable = commands.add_parser(
        "chat-setup-enable",
        help="Turn on chat-driven setup: the setup connector and the owner code",
    )
    enable.add_argument(
        "--rotate-owner-code", action="store_true", help="Make a new owner code (the old one stops)"
    )
    listing = commands.add_parser("proposals", help="List what Muse has asked for")
    listing.add_argument("--all", action="store_true", help="Include finished proposals")
    link = commands.add_parser("proposal-link", help="A fresh review link for a waiting proposal")
    link.add_argument("--proposal-id", required=True)
    commands.add_parser("audit-verify", help="Check the hash chain over the administrative audit")
    call = commands.add_parser("explain-call", help="Trace one call back to the request behind it")
    call.add_argument("--request-id", required=True)
    change = commands.add_parser("explain-change", help="Show the record of one proposal")
    change.add_argument("--proposal-id", required=True)


def run(
    args: argparse.Namespace,
    repository: SqliteControlPlaneRepository,
    config: ServeConfig,
    policy: EndpointSecurityPolicy,
) -> object:
    store = SetupStore(repository, open_secret_box(args.database))
    if args.command == "chat-setup-enable":
        return _enable(args, repository, config, policy, store)
    if args.command == "proposals":
        states = None if args.all else ("waiting_for_owner", "connecting", "in_review")
        return [_summary(item) for item in store.list_proposals(states=states)]
    if args.command == "proposal-link":
        token = secrets.token_urlsafe(32)
        store.reissue_token(args.proposal_id, _hash(token), _now())
        return {
            "proposalId": args.proposal_id,
            "reviewLink": f"{config.public_url}/review/{token}",
            "note": "The earlier link no longer works. Open this one on your own device.",
        }
    if args.command == "audit-verify":
        report = repository.verify_admin_audit_chain()
        result: dict[str, object] = {
            "ok": report.ok,
            "chainedRows": report.chained,
            "olderRowsWithoutHash": report.unchained,
            "head": report.head,
            "note": "Keep the head somewhere else: it is what shows that rows were not cut off.",
        }
        if not report.ok:
            raise InvalidStateError(
                f"The audit chain is broken at event {report.bad_event_id}: {report.problem}"
            )
        return result
    if args.command == "explain-call":
        return _explain_call(args.request_id, repository, store)
    if args.command == "explain-change":
        return _record(store.get_proposal(args.proposal_id))
    raise AssertionError(f"Unhandled command {args.command}")


def _enable(
    args: argparse.Namespace,
    repository: SqliteControlPlaneRepository,
    config: ServeConfig,
    policy: EndpointSecurityPolicy,
    store: SetupStore,
) -> dict[str, object]:
    now = _now()
    control = ControlPlaneService(repository, endpoint_policy=policy)
    version = ensure_setup_server(repository, control, now)
    connections = ConnectionService(repository)
    result: dict[str, object] = {"setupCatalog": version, "serviceConfigured": config.chat_setup}
    live = [item for item in connections.list(SETUP_SERVER_ID) if item.revoked_at is None]
    if live:
        result["connectorUrl"] = _connector_url(config.public_url, live[0])
        result["connectorNote"] = (
            "The setup connector already exists. Its token was shown when it was created; "
            f"make a new one with: rotate-token --connection-id {live[0].connection_id}"
        )
    else:
        linked = connections.link(
            server_id=SETUP_SERVER_ID,
            label=SETUP_LABEL,
            credential_kind="none",
            actor=args.actor,
        )
        url = _connector_url(config.public_url, linked.record)
        result["connectorUrl"] = url
        result["connectionId"] = linked.record.connection_id
        result["token"] = linked.token
        result["connectionCode"] = linked.link_code
        result["museInstructions"] = (
            f'Tell Muse: "Create a connector called {SETUP_LABEL} from the API spec at {url}." '
            f"If Muse asks for a bearer token, give it: {linked.token}"
        )
    if store.owner_code() is None or args.rotate_owner_code:
        rotated = store.owner_code() is not None
        code = owner.new_owner_code()
        salt = owner.new_salt()
        store.set_owner_code(owner.hash_code(code, salt), salt, now)
        repository.record_admin_audit(
            occurred_at=now,
            actor=args.actor,
            action="setup.owner_code.rotated" if rotated else "setup.owner_code.created",
            server_id=None,
            data={},
        )
        store.drop_sessions()
        result["ownerCode"] = code
        result["ownerCodeNote"] = (
            "Shown once. Keep it in your password manager. You type it only on the adapter's own "
            "review pages, never into Muse."
        )
    else:
        result["ownerCodeNote"] = (
            "An owner code is already set. Use --rotate-owner-code to make a new one."
        )
    if not config.chat_setup:
        result["warning"] = (
            'The review pages are off. Set "chatSetup": true in the service configuration and '
            "restart the service."
        )
    return result


def _explain_call(
    request_id: str, repository: SqliteControlPlaneRepository, store: SetupStore
) -> dict[str, object]:
    rows = repository.runtime_audit_for_request(request_id)
    if not rows:
        raise EntityNotFoundError(f"No call with request id {request_id} was recorded")
    call = dict(rows[0])
    server_id = str(call["server_id"])
    version = call.get("catalog_version")
    result: dict[str, object] = {"call": call}
    if not isinstance(version, str):
        return result
    catalog = repository.get_catalog(server_id, version)
    result["catalog"] = {
        "serverId": server_id,
        "version": version,
        "digest": catalog.catalog_digest,
        "publishedAt": catalog.published_at,
    }
    origin = store.provenance(server_id, version)
    result["origin"] = origin
    if origin and origin.get("proposalId"):
        result["proposal"] = _record(store.get_proposal(str(origin["proposalId"])))
    else:
        result["proposal"] = None
        result["note"] = "This catalog was published at the command line: no chat request exists."
    return result


def _record(proposal: Proposal) -> dict[str, object]:
    return {
        "proposalId": proposal.proposal_id,
        "kind": proposal.kind,
        "state": proposal.state,
        "userWords": proposal.user_words,
        "request": dict(proposal.request),
        "serverId": proposal.target_server_id,
        "connectionId": proposal.target_connection_id,
        "snapshotId": proposal.snapshot_id,
        "digest": proposal.digest,
        "decision": dict(proposal.decision) if proposal.decision is not None else None,
        "applied": dict(proposal.applied) if proposal.applied is not None else None,
        "createdAt": proposal.created_at,
        "updatedAt": proposal.updated_at,
    }


def _summary(proposal: Proposal) -> dict[str, object]:
    return {
        "proposalId": proposal.proposal_id,
        "kind": proposal.kind,
        "state": proposal.state,
        "serverId": proposal.target_server_id,
        "userWords": proposal.user_words[:120],
        "createdAt": proposal.created_at,
        "expiresAt": proposal.expires_at,
    }


def _connector_url(public_url: str, record: ConnectionRecord) -> str:
    return f"{public_url.rstrip('/')}/c/{record.slug}/openapi.json"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
