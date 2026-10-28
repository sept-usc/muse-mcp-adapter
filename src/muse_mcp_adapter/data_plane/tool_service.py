import functools
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import uuid4

from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for

from ..catalog.canonical import canonical_json, digest, freeze_json, thaw_json
from ..credentials.resolver import CredentialResolver
from ..domain.errors import (
    ApprovalDeniedError,
    ApprovalRequiredError,
    ConflictError,
    CredentialResolutionError,
    DownstreamAuthRejectedError,
    DownstreamContentError,
    DownstreamToolError,
    DownstreamUnavailableError,
    HandleNotAllowedError,
    HandleUnknownError,
    IdempotencyConflictError,
    InputRequiredError,
    InvalidStateError,
    OperationCancelledError,
    OperationInProgressError,
    OperationNotFoundError,
    PublishedPolicyError,
    ReauthorizationRequiredError,
    RequestValidationError,
    ServerDisabledError,
    UnknownOutcomeError,
    UnsupportedPrimitiveError,
)
from ..domain.models import (
    DataPlaneRequest,
    DataPlaneResponse,
    McpToolResult,
    NormalizedContent,
    OperationRecord,
    PublishedPrimitive,
)
from ..operations.cancellation import CancellationRegistry
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from .approval import ApprovalVerifier
from .content import (
    DEFAULT_TOOL_RESULT_TYPES,
    mime_allowed,
    narrow_types,
    normalize_block,
    too_large,
)
from .owner_gate import REVEAL_OPERATION_ID, OwnerGate, check_arguments
from .release import ReleaseEngine
from .serde import response_from_dict, response_to_dict
from .tool_client import (
    McpAuthRejected,
    McpCancelled,
    McpInputRequired,
    McpPreDispatchError,
    McpReauthorizationRequired,
    McpToolClient,
    McpToolExecutionFailure,
    McpUnknownOutcome,
)

PENDING = "PENDING"
RUNNING = "RUNNING"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
INPUT_REQUIRED = "INPUT_REQUIRED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
CANCELLED = "CANCELLED"

MAX_ARGUMENT_BYTES = 1024 * 1024
MAX_PROGRESS_EVENTS = 200
MAX_PROGRESS_MESSAGE_CHARS = 500


@dataclass(frozen=True, slots=True)
class _ToolPolicy:
    """Published policy values parsed up front so a bad policy never strands an operation."""

    required_scopes: frozenset[str]
    timeout_seconds: int
    max_result_bytes: int
    result_content_types: frozenset[str]


class ToolDataPlaneService:
    """Executes published tools.

    Writes need an idempotency key. By default a write is authorized by Muse's own confirmation
    with its user. Pass an ``approval_verifier`` to additionally require signed, single-use
    approval evidence bound to the exact action.
    """

    def __init__(
        self,
        repository: SqliteControlPlaneRepository,
        credential_resolver: CredentialResolver,
        mcp_client: McpToolClient,
        *,
        approval_verifier: ApprovalVerifier | None = None,
        owner_gate: OwnerGate | None = None,
        approval_link: Callable[[str], str] | None = None,
        on_held: Callable[[str, Sequence[str], str, str], None] | None = None,
        release: ReleaseEngine | None = None,
        clock: Callable[[], str],
        operation_id_factory: Callable[[], str] | None = None,
        cancellation_registry: CancellationRegistry | None = None,
    ) -> None:
        self._repository = repository
        self._credential_resolver = credential_resolver
        self._mcp_client = mcp_client
        self._approval_verifier = approval_verifier
        self._owner_gate = owner_gate
        self._approval_link = approval_link
        self._on_held = on_held
        self._release = release
        self._clock = clock
        self._operation_id_factory = operation_id_factory or (lambda: str(uuid4()))
        self._cancellations = cancellation_registry or CancellationRegistry()

    def execute(self, request: DataPlaneRequest) -> DataPlaneResponse:
        _validate_request(request)
        catalog_version, endpoint, operation, policy = self._resolve(request)
        if self._release is not None:
            request = self._release.open_request(request, operation)
        _validate_schema(
            schema=operation.definition.get("inputSchema"),
            instance=request.arguments,
            label="Tool input",
        )
        check_arguments(operation, request.arguments)
        if operation.classification != "read" and not request.idempotency_key:
            raise RequestValidationError("Write tools require an idempotency key")

        action_digest = _action_digest(
            request,
            catalog_version=catalog_version,
            definition_digest=operation.definition_digest,
        )
        existing = self._find_existing(request)
        if existing is not None:
            return self._resume_existing(
                existing=existing,
                request=request,
                operation=operation,
                policy=policy,
                endpoint=endpoint,
                action_digest=action_digest,
            )

        now = self._clock()
        record = OperationRecord(
            operation_id=self._operation_id_factory(),
            request_id=request.request_id,
            server_id=request.server_id,
            catalog_version=catalog_version,
            public_operation_id=request.public_operation_id,
            local_identity_id=request.local_identity_id,
            connection_id=request.connection_id,
            classification=operation.classification,
            status=PENDING,
            idempotency_key=request.idempotency_key,
            request_digest=action_digest,
            approval_id=None,
            created_at=now,
            updated_at=now,
            result=None,
            error_code=None,
            error_message=None,
            retryable=False,
        )
        try:
            self._repository.create_operation(record)
        except ConflictError:
            raced = self._find_existing(request)
            if raced is None:
                raise
            return self._resume_existing(
                existing=raced,
                request=request,
                operation=operation,
                policy=policy,
                endpoint=endpoint,
                action_digest=action_digest,
            )

        approval_id = self._authorize(
            request, operation, action_digest, record.operation_id, now, from_status=PENDING
        )
        running = self._repository.transition_operation(
            record.operation_id,
            expected_statuses=(PENDING,),
            status=RUNNING,
            updated_at=now,
            approval_id=approval_id,
        )
        return self._invoke(
            operation_record=running,
            request=request,
            operation=operation,
            policy=policy,
            endpoint=endpoint,
        )

    def resume_with_input(
        self,
        request: DataPlaneRequest,
        *,
        operation_id: str,
        inputs: Mapping[str, object],
    ) -> DataPlaneResponse:
        """Continue an INPUT_REQUIRED operation by re-running the call with the user's answer."""
        _validate_request(request)
        catalog_version, endpoint, operation, policy = self._resolve(request)
        if self._release is not None:
            request = self._release.open_request(request, operation)
        existing = self._repository.get_operation(operation_id)
        action_digest = _action_digest(
            request,
            catalog_version=catalog_version,
            definition_digest=operation.definition_digest,
        )
        if (
            existing.server_id != request.server_id
            or existing.local_identity_id != request.local_identity_id
            or existing.connection_id != request.connection_id
            or existing.request_digest != action_digest
        ):
            raise IdempotencyConflictError("The operation does not belong to this request")
        if existing.status != INPUT_REQUIRED:
            raise InvalidStateError(
                f"Operation {operation_id} is {existing.status}, not {INPUT_REQUIRED}"
            )
        schema = self._pending_input_schema(operation_id)
        _validate_schema(schema=schema, instance=inputs, label="Elicited input")
        now = self._clock()
        self._repository.append_operation_event(
            operation_id,
            event_type="input_received",
            occurred_at=now,
            data={"fields": sorted(inputs)},
        )
        running = self._repository.transition_operation(
            operation_id,
            expected_statuses=(INPUT_REQUIRED,),
            status=RUNNING,
            updated_at=now,
        )
        return self._invoke(
            operation_record=running,
            request=request,
            operation=operation,
            policy=policy,
            endpoint=endpoint,
            elicitation_answer=inputs,
        )

    def get_operation(self, operation_id: str) -> OperationRecord:
        return self._repository.get_operation(operation_id)

    def reconcile_unknown(
        self,
        operation_id: str,
        *,
        actor: str,
        outcome: str,
        response: DataPlaneResponse | None = None,
    ) -> OperationRecord:
        if not actor:
            raise RequestValidationError("Reconciliation requires an actor")
        now = self._clock()
        if outcome == "not_applied":
            record = self._repository.transition_operation(
                operation_id,
                expected_statuses=(UNKNOWN,),
                status=FAILED,
                updated_at=now,
                error_code="CONFIRMED_NOT_APPLIED",
                error_message="Downstream status confirmed that the write was not applied",
                retryable=True,
            )
        elif outcome == "completed" and response is not None:
            operation = self._repository.get_operation(operation_id)
            if (
                response.operation_id != operation_id
                or response.public_operation_id != operation.public_operation_id
                or response.catalog_version != operation.catalog_version
            ):
                raise RequestValidationError(
                    "Reconciled response does not belong to the unknown operation"
                )
            self._revalidate_reconciled(operation, response)
            record = self._repository.transition_operation(
                operation_id,
                expected_statuses=(UNKNOWN,),
                status=COMPLETED,
                updated_at=now,
                result=response_to_dict(response),
            )
        else:
            raise RequestValidationError(
                "Unknown outcome reconciliation requires not_applied or completed with a response"
            )
        self._repository.append_operation_event(
            operation_id,
            event_type="reconciled",
            occurred_at=now,
            data={"actor": actor, "outcome": outcome},
        )
        return record

    def reveal(self, request: DataPlaneRequest) -> DataPlaneResponse:
        """Show the agent one sealed value, once the owner has approved that one value.

        The owner chooses which labels may be revealed at all (``release.reveal``). Even then the
        value is shown only after the owner approves this handle on their own page, once.
        """
        if self._release is None or self._owner_gate is None:
            raise OperationNotFoundError("Revealing sealed values is not available here")
        handle = request.arguments.get("handle")
        if not isinstance(handle, str) or set(request.arguments) != {"handle"}:
            raise RequestValidationError(
                f'{REVEAL_OPERATION_ID} takes exactly one argument: {{"handle": "[label:id]"}}'
            )
        catalog = self._repository.get_active_catalog(request.server_id)
        allowed = self._release.reveal_labels(catalog.primitives)
        if not allowed:
            raise OperationNotFoundError("The owner has not allowed any sealed value to be shown")
        found = self._release.find(request.connection_id, handle)
        if found is None:
            raise HandleUnknownError(
                f"{handle} is not a value this connection was given, or it has expired. Do not "
                "make up or edit sealed values: read them again from the source."
            )
        label, value, handle_id = found
        if label not in allowed:
            self._release.note(
                "refused",
                connection_id=request.connection_id,
                operation=REVEAL_OPERATION_ID,
                label=label,
                detail=f"{handle_id} reveal not allowed",
            )
            raise HandleNotAllowedError(
                f"The owner has not allowed {label} to be shown to you. Tell the user you "
                "cannot read this value."
            )
        now = self._clock()
        operation_id = self._operation_id_factory()
        decision = self._owner_gate.decide_reveal(
            connection_id=request.connection_id,
            server_id=request.server_id,
            handle=handle.strip(),
            label=label,
            operation_id=operation_id,
            now=now,
        )
        if decision.outcome == "denied":
            self._release.note(
                "refused",
                connection_id=request.connection_id,
                operation=REVEAL_OPERATION_ID,
                label=label,
                detail=f"{handle_id} declined by the owner",
            )
            raise ApprovalDeniedError(
                "The owner looked at this request and declined it. Do not ask again or try to "
                "get around it: tell the user you could not read the value."
            )
        if decision.outcome == "wait":
            assert decision.request is not None
            if decision.owner_token is not None and self._on_held and self._approval_link:
                self._on_held(
                    REVEAL_OPERATION_ID,
                    decision.reasons,
                    self._approval_link(decision.owner_token),
                    decision.request.expires_at,
                )
            url = (
                self._approval_link(decision.token)
                if decision.token is not None and self._approval_link is not None
                else None
            )
            raise ApprovalRequiredError(
                _owner_message(REVEAL_OPERATION_ID, decision.reasons, url),
                operation_id=operation_id,
                action_digest=decision.request.content_digest,
                approval_url=url,
                reasons=decision.reasons,
            )
        self._release.note(
            "revealed",
            connection_id=request.connection_id,
            operation=REVEAL_OPERATION_ID,
            label=label,
            detail=handle_id,
        )
        provenance = cast(
            Mapping[str, object],
            freeze_json({"serverId": request.server_id, "kind": "reveal", "label": label}),
        )
        return DataPlaneResponse(
            request_id=request.request_id,
            catalog_version=catalog.catalog_version,
            public_operation_id=REVEAL_OPERATION_ID,
            primitive_type="tool",
            provenance=provenance,
            operation_id=operation_id,
            operation_status=COMPLETED,
            structured_content=cast(
                Mapping[str, object], freeze_json({"label": label, "value": value})
            ),
        )

    def _resolve(
        self, request: DataPlaneRequest
    ) -> tuple[str, str, PublishedPrimitive, _ToolPolicy]:
        server = self._repository.get_server(request.server_id)
        if not server.enabled:
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
        if operation.primitive_type != "tool":
            raise UnsupportedPrimitiveError(
                f"Primitive type {operation.primitive_type} is not a tool"
            )
        if len(canonical_json(request.arguments).encode()) > MAX_ARGUMENT_BYTES:
            raise RequestValidationError("Tool arguments exceed the maximum request size")
        endpoint = _required_string(catalog.server, "endpoint")
        return catalog.catalog_version, endpoint, operation, _parse_policy(operation)

    def _find_existing(self, request: DataPlaneRequest) -> OperationRecord | None:
        if request.idempotency_key is None:
            return None
        return self._repository.find_operation_by_idempotency(
            server_id=request.server_id,
            public_operation_id=request.public_operation_id,
            local_identity_id=request.local_identity_id,
            connection_id=request.connection_id,
            idempotency_key=request.idempotency_key,
        )

    def _authorize(
        self,
        request: DataPlaneRequest,
        operation: PublishedPrimitive,
        action_digest: str,
        operation_id: str,
        now: str,
        *,
        from_status: str,
    ) -> str | None:
        if operation.classification == "read":
            return None
        if self._owner_gate is not None:
            return self._authorize_by_owner(
                request, operation, operation_id, now, from_status=from_status
            )
        if self._approval_verifier is None:
            # Muse asks its user before consequential actions and only then calls the write
            # operation, so the call itself is the confirmation. The request ID ties the
            # recorded approval to this exact call for audit.
            return f"muse-confirmed:{request.request_id}"
        evidence = request.approval
        reason: str | None
        if evidence is None:
            reason = "Approval evidence is required for write tools"
        else:
            reason = self._approval_verifier.verify(
                evidence,
                classification=operation.classification,
                action_digest=action_digest,
                now=now,
            )
            if reason is None:
                try:
                    self._repository.consume_approval(evidence.approval_id, operation_id, now)
                except ConflictError:
                    reason = "Approval evidence has already been used for another operation"
        if reason is None:
            assert evidence is not None
            return evidence.approval_id
        if from_status == PENDING:
            self._repository.transition_operation(
                operation_id,
                expected_statuses=(PENDING,),
                status=APPROVAL_REQUIRED,
                updated_at=now,
            )
        raise ApprovalRequiredError(reason, operation_id=operation_id, action_digest=action_digest)

    def _authorize_by_owner(
        self,
        request: DataPlaneRequest,
        operation: PublishedPrimitive,
        operation_id: str,
        now: str,
        *,
        from_status: str,
    ) -> str:
        """Run a write only when the owner's gate lets it through; otherwise hold or refuse it."""
        assert self._owner_gate is not None
        content = _content_digest(request, definition_digest=operation.definition_digest)
        decision = self._owner_gate.decide(
            primitive=operation,
            server_id=request.server_id,
            connection_id=request.connection_id,
            arguments=request.arguments,
            content_digest=content,
            operation_id=operation_id,
            now=now,
        )
        if decision.outcome == "run":
            assert decision.approval_id is not None
            return decision.approval_id
        if decision.outcome == "denied":
            self._repository.transition_operation(
                operation_id,
                expected_statuses=(from_status,),
                status=FAILED,
                updated_at=now,
                error_code=ApprovalDeniedError.code,
                error_message="The owner declined this action",
                retryable=False,
            )
            raise ApprovalDeniedError(
                "The owner looked at this action and declined it. Do not try it again or try to "
                "get around it: tell the user it was declined."
            )
        if from_status == PENDING:
            self._repository.transition_operation(
                operation_id,
                expected_statuses=(PENDING,),
                status=APPROVAL_REQUIRED,
                updated_at=now,
            )
        if decision.owner_token is not None and self._on_held and self._approval_link:
            assert decision.request is not None
            self._on_held(
                operation.public_operation_id,
                decision.reasons,
                self._approval_link(decision.owner_token),
                decision.request.expires_at,
            )
        url = (
            self._approval_link(decision.token)
            if decision.token is not None and self._approval_link is not None
            else None
        )
        raise ApprovalRequiredError(
            _owner_message(operation.public_operation_id, decision.reasons, url),
            operation_id=operation_id,
            action_digest=content,
            approval_url=url,
            reasons=decision.reasons,
        )

    def _resume_existing(
        self,
        *,
        existing: OperationRecord,
        request: DataPlaneRequest,
        operation: PublishedPrimitive,
        policy: _ToolPolicy,
        endpoint: str,
        action_digest: str,
    ) -> DataPlaneResponse:
        if existing.request_digest != action_digest:
            raise IdempotencyConflictError(
                "The idempotency key is already bound to a different request"
            )
        if existing.status == COMPLETED:
            if existing.result is None:
                raise DownstreamToolError("Completed operation has no stored result")
            return replace(response_from_dict(existing.result), replayed=True)
        if existing.status == UNKNOWN:
            raise UnknownOutcomeError(
                "The prior write may have completed; reconcile its status before retrying",
                operation_id=existing.operation_id,
            )
        if existing.status in (PENDING, RUNNING):
            raise OperationInProgressError(
                f"Operation {existing.operation_id} is already {existing.status}"
            )
        if existing.status == INPUT_REQUIRED:
            raise InputRequiredError(
                "The operation is waiting for user input",
                operation_id=existing.operation_id,
                schema=self._pending_input_schema(existing.operation_id),
            )
        if existing.status == CANCELLED:
            raise OperationCancelledError(f"Operation {existing.operation_id} was cancelled")
        if existing.status == FAILED and not existing.retryable:
            raise DownstreamToolError(
                existing.error_message or "The previous tool execution failed"
            )
        if existing.status not in (APPROVAL_REQUIRED, FAILED):
            raise DownstreamToolError(
                f"Operation {existing.operation_id} cannot be resumed from {existing.status}"
            )

        now = self._clock()
        approval_id = self._authorize(
            request,
            operation,
            action_digest,
            existing.operation_id,
            now,
            from_status=existing.status,
        )
        running = self._repository.transition_operation(
            existing.operation_id,
            expected_statuses=(existing.status,),
            status=RUNNING,
            updated_at=now,
            approval_id=approval_id,
        )
        return self._invoke(
            operation_record=running,
            request=request,
            operation=operation,
            policy=policy,
            endpoint=endpoint,
        )

    def _invoke(
        self,
        *,
        operation_record: OperationRecord,
        request: DataPlaneRequest,
        operation: PublishedPrimitive,
        policy: _ToolPolicy,
        endpoint: str,
        elicitation_answer: Mapping[str, object] | None = None,
    ) -> DataPlaneResponse:
        """Run the call and guarantee the operation never stays RUNNING on any exit path."""
        operation_id = operation_record.operation_id
        is_read = operation.classification == "read"
        dispatched = False
        try:
            try:
                credential = self._credential_resolver.resolve(
                    local_identity_id=request.local_identity_id,
                    connection_id=request.connection_id,
                    server_id=request.server_id,
                    required_scopes=policy.required_scopes,
                )
            except CredentialResolutionError as error:
                self._fail(operation_id, code=error.code, message=str(error), retryable=True)
                raise

            dispatched = True
            try:
                with self._cancellations.track(operation_id) as cancellation:
                    result = self._mcp_client.call_tool(
                        endpoint=endpoint,
                        name=operation.mcp_name,
                        arguments=request.arguments,
                        credential=credential,
                        idempotency_key=request.idempotency_key,
                        timeout_seconds=policy.timeout_seconds,
                        progress_callback=lambda progress, total, message: self._record_progress(
                            operation_id,
                            progress=progress,
                            total=total,
                            message=message,
                        ),
                        elicitation_answer=elicitation_answer,
                        log_callback=lambda level, message: self._record_log(
                            operation_id, level=level, message=message
                        ),
                        cancellation=cancellation,
                    )
            except McpReauthorizationRequired as error:
                dispatched = False
                self._fail(
                    operation_id,
                    code=ReauthorizationRequiredError.code,
                    message=str(error),
                    retryable=True,
                )
                raise ReauthorizationRequiredError(str(error)) from error
            except McpAuthRejected as error:
                dispatched = False
                self._fail(
                    operation_id,
                    code=DownstreamAuthRejectedError.code,
                    message=str(error),
                    retryable=True,
                )
                raise DownstreamAuthRejectedError(str(error)) from error
            except McpPreDispatchError as error:
                dispatched = False
                self._fail(
                    operation_id,
                    code="PRE_DISPATCH_FAILURE",
                    message=str(error),
                    retryable=True,
                )
                raise DownstreamUnavailableError(str(error)) from error
            except McpInputRequired as error:
                self._repository.append_operation_event(
                    operation_id,
                    event_type="input_requested",
                    occurred_at=self._clock(),
                    data={"message": str(error), "schema": error.schema},
                )
                self._repository.transition_operation(
                    operation_id,
                    expected_statuses=(RUNNING,),
                    status=INPUT_REQUIRED,
                    updated_at=self._clock(),
                    error_code="INPUT_REQUIRED",
                    error_message=_safe_message(str(error)),
                )
                raise InputRequiredError(
                    str(error), operation_id=operation_id, schema=error.schema
                ) from error
            except McpCancelled as error:
                self._repository.transition_operation(
                    operation_id,
                    expected_statuses=(RUNNING,),
                    status=CANCELLED,
                    updated_at=self._clock(),
                    error_code="CANCELLED",
                    error_message=_safe_message(str(error)),
                )
                raise OperationCancelledError(str(error)) from error
            except McpUnknownOutcome as error:
                self._mark_unknown(
                    operation_id,
                    is_read=is_read,
                    code="DOWNSTREAM_OUTCOME_UNKNOWN",
                    message=str(error),
                )
                if is_read:
                    raise DownstreamToolError(str(error)) from error
                raise UnknownOutcomeError(
                    "The write outcome is unknown and must be reconciled before retry",
                    operation_id=operation_id,
                ) from error
            except McpToolExecutionFailure as error:
                self._fail(operation_id, code=error.code, message=str(error), retryable=False)
                raise DownstreamToolError(str(error)) from error

            if result.is_error:
                message = result.error_message or "The downstream tool reported an error"
                self._fail(
                    operation_id,
                    code=result.error_code or "TOOL_RESULT_ERROR",
                    message=message,
                    retryable=False,
                )
                raise DownstreamToolError(message)

            try:
                response = _normalize_tool_result(
                    request=request,
                    operation=operation,
                    policy=policy,
                    operation_id=operation_id,
                    catalog_version=operation_record.catalog_version,
                    result=result,
                )
            except DownstreamContentError as error:
                # The tool already ran: for a write the side effect may have been applied.
                self._mark_unknown(
                    operation_id,
                    is_read=is_read,
                    code="DOWNSTREAM_RESULT_REJECTED",
                    message=str(error),
                )
                if is_read:
                    raise
                raise UnknownOutcomeError(
                    "The write ran but its result was rejected; reconcile before retry: "
                    + _safe_message(str(error)),
                    operation_id=operation_id,
                ) from error

            if self._release is not None:
                response = self._release.seal_response(request, operation, response)
            self._complete(operation_id, response, is_read=is_read)
            return response
        except BaseException:
            # Known failures above already moved the operation; only repair a leaked RUNNING.
            self._repair_running(operation_id, is_read=is_read, dispatched=dispatched)
            raise

    def _complete(self, operation_id: str, response: DataPlaneResponse, *, is_read: bool) -> None:
        try:
            self._repository.transition_operation(
                operation_id,
                expected_statuses=(RUNNING,),
                status=COMPLETED,
                updated_at=self._clock(),
                result=response_to_dict(response),
            )
        except InvalidStateError:
            current = self._repository.get_operation(operation_id)
            if current.status == CANCELLED and is_read:
                raise OperationCancelledError(
                    f"Operation {operation_id} was cancelled before it completed"
                ) from None
            raise

    def _mark_unknown(self, operation_id: str, *, is_read: bool, code: str, message: str) -> None:
        if is_read:
            self._fail(operation_id, code=code, message=message, retryable=True)
            return
        self._repository.transition_operation(
            operation_id,
            expected_statuses=(RUNNING,),
            status=UNKNOWN,
            updated_at=self._clock(),
            error_code=code,
            error_message=_safe_message(message),
        )

    def _repair_running(self, operation_id: str, *, is_read: bool, dispatched: bool) -> None:
        """Move an operation stranded in RUNNING by an unexpected error to a safe state."""
        try:
            current = self._repository.get_operation(operation_id)
            if current.status != RUNNING:
                return
            if is_read or not dispatched:
                self._fail(
                    operation_id,
                    code="INTERNAL_ERROR",
                    message="The adapter failed while executing the operation",
                    retryable=True,
                )
            else:
                self._mark_unknown(
                    operation_id,
                    is_read=False,
                    code="INTERNAL_ERROR_AFTER_DISPATCH",
                    message="The adapter failed after the write was dispatched",
                )
        except InvalidStateError:
            return

    def _pending_input_schema(self, operation_id: str) -> Mapping[str, object]:
        for event in reversed(self._repository.list_operation_events(operation_id)):
            if event.event_type == "input_requested":
                schema = event.data.get("schema")
                if isinstance(schema, Mapping):
                    return cast(Mapping[str, object], schema)
        raise InvalidStateError(f"Operation {operation_id} has no pending input request")

    def _revalidate_reconciled(
        self, operation: OperationRecord, response: DataPlaneResponse
    ) -> None:
        """Apply the published output, content-type and size policy to operator-supplied results."""
        catalog = self._repository.get_catalog(operation.server_id, operation.catalog_version)
        primitive = next(
            (
                item
                for item in catalog.primitives
                if item.public_operation_id == operation.public_operation_id
            ),
            None,
        )
        if primitive is None:
            raise RequestValidationError("Reconciled operation is not in its catalog version")
        policy = _parse_policy(primitive)
        output_schema = primitive.definition.get("outputSchema")
        if output_schema is not None:
            if response.structured_content is None:
                raise RequestValidationError("Reconciled response lacks structured content")
            _validate_schema(
                schema=output_schema, instance=response.structured_content, label="Tool output"
            )
        total = (
            len(canonical_json(response.structured_content).encode())
            if response.structured_content is not None
            else 0
        )
        for content in response.contents:
            if not mime_allowed(content.mime_type, policy.result_content_types):
                raise RequestValidationError(
                    f"Reconciled content type {content.mime_type} is not allowed"
                )
            total += len(content.text.encode())
        if total > policy.max_result_bytes:
            raise RequestValidationError("Reconciled response exceeds the published size limit")

    def _record_progress(
        self,
        operation_id: str,
        *,
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        if self._repository.count_operation_events(operation_id, "progress") >= MAX_PROGRESS_EVENTS:
            return
        data: dict[str, object] = {"progress": progress}
        if total is not None:
            data["total"] = total
        if message is not None:
            data["message"] = message[:MAX_PROGRESS_MESSAGE_CHARS]
        self._repository.append_operation_event(
            operation_id,
            event_type="progress",
            occurred_at=self._clock(),
            data=data,
        )

    def _record_log(self, operation_id: str, *, level: str, message: str) -> None:
        """Keep the server's log notifications so the caller can read them with the operation."""
        if self._repository.count_operation_events(operation_id, "log") >= MAX_PROGRESS_EVENTS:
            return
        self._repository.append_operation_event(
            operation_id,
            event_type="log",
            occurred_at=self._clock(),
            data={"level": level[:20], "message": message[:MAX_PROGRESS_MESSAGE_CHARS]},
        )

    def _fail(
        self,
        operation_id: str,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        self._repository.transition_operation(
            operation_id,
            expected_statuses=(RUNNING,),
            status=FAILED,
            updated_at=self._clock(),
            error_code=code,
            error_message=_safe_message(message),
            retryable=retryable,
        )


def _validate_request(request: DataPlaneRequest) -> None:
    fields = {
        "request_id": request.request_id,
        "server_id": request.server_id,
        "public_operation_id": request.public_operation_id,
        "local_identity_id": request.local_identity_id,
        "connection_id": request.connection_id,
    }
    missing = [name for name, value in fields.items() if not value]
    if missing:
        raise RequestValidationError("Missing request fields: " + ", ".join(missing))


def _action_digest(
    request: DataPlaneRequest, *, catalog_version: str, definition_digest: str
) -> str:
    return digest(
        {
            "serverId": request.server_id,
            "catalogVersion": catalog_version,
            "definitionDigest": definition_digest,
            "publicOperationId": request.public_operation_id,
            "localIdentityId": request.local_identity_id,
            "connectionId": request.connection_id,
            "idempotencyKey": request.idempotency_key,
            "arguments": request.arguments,
        }
    )


def _content_digest(request: DataPlaneRequest, *, definition_digest: str) -> str:
    """What an owner's approval is bound to: this tool, this connection, these exact arguments.

    The idempotency key is left out on purpose, so an agent that repeats the call with a new key
    after approval is still the same action, and an approval is spent once either way.
    """
    return digest(
        {
            "serverId": request.server_id,
            "definitionDigest": definition_digest,
            "publicOperationId": request.public_operation_id,
            "connectionId": request.connection_id,
            "arguments": request.arguments,
        }
    )


def _owner_message(operation: str, reasons: Sequence[str], url: str | None) -> str:
    why = " ".join(f"{reason}." for reason in reasons)
    if url is None:
        return (
            f"This action ({operation}) is waiting for the owner's approval. {why} Do not repeat "
            "it yet: tell the user it is waiting, and call it again with exactly the same "
            "arguments once they say they approved it."
        )
    return (
        f"This action ({operation}) needs the owner's approval before it can run. {why} "
        f"Tell the user to open this one-time link, check the action and approve it with "
        f"their approval passcode: {url} . Never enter or ask for the passcode yourself. After "
        "they say they approved it, call this operation again with exactly the same arguments. "
        "If they decline, do not try again."
    )


def _parse_policy(operation: PublishedPrimitive) -> _ToolPolicy:
    policy = operation.policy
    scopes = policy.get("requiredScopes", ())
    timeout = policy.get("timeoutSeconds")
    max_bytes = policy.get("maxResultBytes")
    if not _is_str_sequence(scopes):
        raise PublishedPolicyError("Published requiredScopes is invalid")
    if not _is_int(timeout) or not 1 <= cast(int, timeout) <= 300:
        raise PublishedPolicyError("Published timeoutSeconds is invalid")
    if not _is_int(max_bytes) or cast(int, max_bytes) < 1:
        raise PublishedPolicyError("Published maxResultBytes is invalid")
    declared = operation.definition.get("resultContentTypes", DEFAULT_TOOL_RESULT_TYPES)
    if not _is_str_sequence(declared):
        raise PublishedPolicyError("Published resultContentTypes is invalid")
    allowed = frozenset(cast(Sequence[str], declared))
    narrowed = policy.get("allowedResultContentTypes")
    if narrowed is not None:
        if not _is_str_sequence(narrowed):
            raise PublishedPolicyError("Published allowedResultContentTypes is invalid")
        allowed = narrow_types(allowed, cast(Sequence[str], narrowed))
    return _ToolPolicy(
        required_scopes=frozenset(cast(Sequence[str], scopes)),
        timeout_seconds=cast(int, timeout),
        max_result_bytes=cast(int, max_bytes),
        result_content_types=allowed,
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str_sequence(value: object) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and all(isinstance(item, str) and item for item in cast(Sequence[object], value))
    )


@functools.lru_cache(maxsize=256)
def _compiled_validator(schema_json: str) -> Validator:
    schema = cast(dict[str, Any], json.loads(schema_json))
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    return validator_class(schema)


def _validate_schema(*, schema: object, instance: object, label: str) -> None:
    schema_value = thaw_json(schema)
    instance_value: Any = thaw_json(instance)
    if not isinstance(schema_value, dict):
        raise DownstreamContentError(f"Published {label.lower()} schema is invalid")
    try:
        validator = _compiled_validator(canonical_json(cast(dict[str, Any], schema_value)))
        validator.validate(instance_value)
    except SchemaError as error:
        raise DownstreamContentError(f"Published {label.lower()} schema is invalid") from error
    except ValidationError as error:
        path = ".".join(str(item) for item in error.absolute_path) or "$"
        raise RequestValidationError(
            f"{label} validation failed at {path}: {error.message}"
        ) from error


def _normalize_tool_result(
    *,
    request: DataPlaneRequest,
    operation: PublishedPrimitive,
    policy: _ToolPolicy,
    operation_id: str,
    catalog_version: str,
    result: McpToolResult,
) -> DataPlaneResponse:
    output_schema = operation.definition.get("outputSchema")
    if output_schema is not None:
        if result.structured_content is None:
            raise DownstreamContentError("Tool result is missing required structured content")
        try:
            _validate_schema(
                schema=output_schema,
                instance=result.structured_content,
                label="Tool output",
            )
        except RequestValidationError as error:
            raise DownstreamContentError(str(error)) from error

    try:
        total_bytes = (
            len(canonical_json(result.structured_content).encode())
            if result.structured_content is not None
            else 0
        )
    except TypeError as error:
        raise DownstreamContentError("Tool structured content is not valid JSON") from error
    contents: list[NormalizedContent] = []
    for block in result.content:
        content, size = normalize_block(block, policy.result_content_types, noun="Tool")
        total_bytes += size
        if total_bytes > policy.max_result_bytes:
            raise DownstreamContentError(too_large("Tool result", policy.max_result_bytes))
        contents.append(content)
    if total_bytes > policy.max_result_bytes:
        raise DownstreamContentError(too_large("Tool result", policy.max_result_bytes))

    structured = (
        cast(Mapping[str, object], freeze_json(result.structured_content))
        if result.structured_content is not None
        else None
    )
    provenance = cast(
        Mapping[str, object],
        freeze_json(
            {
                "serverId": request.server_id,
                "catalogVersion": catalog_version,
                "primitiveKey": operation.primitive_key,
                "mcpName": operation.mcp_name,
                "definitionDigest": operation.definition_digest,
            }
        ),
    )
    return DataPlaneResponse(
        request_id=request.request_id,
        catalog_version=catalog_version,
        public_operation_id=operation.public_operation_id,
        primitive_type="tool",
        provenance=provenance,
        operation_id=operation_id,
        operation_status=COMPLETED,
        structured_content=structured,
        contents=tuple(contents),
    )


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise DownstreamContentError(f"Published catalog field {key} is invalid")
    return item


def _safe_message(value: str) -> str:
    return value[:1000]
