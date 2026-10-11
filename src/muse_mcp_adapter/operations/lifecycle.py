from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for

from ..catalog.canonical import thaw_json
from ..domain.errors import InvalidStateError, RequestValidationError
from ..domain.models import OperationRecord
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from .cancellation import CancellationRegistry

MAX_PROGRESS_EVENTS = 200
MAX_PROGRESS_MESSAGE_CHARS = 500


class OperationLifecycleService:
    """Durable lifecycle transitions used by async jobs and elicitation handlers."""

    def __init__(
        self,
        repository: SqliteControlPlaneRepository,
        *,
        clock: Callable[[], str],
        cancellation_registry: CancellationRegistry | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._cancellations = cancellation_registry

    def request_input(
        self,
        operation_id: str,
        *,
        schema: Mapping[str, object],
        message: str,
    ) -> OperationRecord:
        now = self._clock()
        operation = self._repository.transition_operation(
            operation_id,
            expected_statuses=("RUNNING",),
            status="INPUT_REQUIRED",
            updated_at=now,
            error_code="INPUT_REQUIRED",
            error_message=message,
        )
        self._repository.append_operation_event(
            operation_id,
            event_type="input_requested",
            occurred_at=now,
            data={"message": message, "schema": schema},
        )
        return operation

    def resume(self, operation_id: str, *, inputs: Mapping[str, object]) -> OperationRecord:
        """Validate user input against the schema that was requested, then resume the operation."""
        schema = self._requested_schema(operation_id)
        try:
            validator_class = validator_for(schema)
            validator_class.check_schema(schema)
            instance: Any = thaw_json(inputs)
            validator_class(schema).validate(instance)
        except SchemaError as error:
            raise InvalidStateError("The stored input schema is invalid") from error
        except ValidationError as error:
            raise RequestValidationError(f"Input validation failed: {error.message}") from error
        now = self._clock()
        operation = self._repository.transition_operation(
            operation_id,
            expected_statuses=("INPUT_REQUIRED",),
            status="RUNNING",
            updated_at=now,
        )
        self._repository.append_operation_event(
            operation_id,
            event_type="input_received",
            occurred_at=now,
            data={"fields": sorted(inputs)},
        )
        return operation

    def cancel(self, operation_id: str, *, reason: str) -> OperationRecord:
        """Cancel an operation that has not dispatched, or a running read.

        A running write cannot be rolled back, so cancellation is recorded as a request only and
        the operation still resolves to its real outcome.
        """
        now = self._clock()
        current = self._repository.get_operation(operation_id)
        if current.status == "RUNNING":
            self._repository.append_operation_event(
                operation_id,
                event_type="cancel_requested",
                occurred_at=now,
                data={"reason": reason},
            )
            if current.classification == "read" and self._cancellations is not None:
                self._cancellations.request(operation_id)
            return current
        operation = self._repository.transition_operation(
            operation_id,
            expected_statuses=(
                "PENDING",
                "RUNNING",
                "APPROVAL_REQUIRED",
                "INPUT_REQUIRED",
            ),
            status="CANCELLED",
            updated_at=now,
            error_code="CANCELLED",
            error_message=reason,
        )
        self._repository.append_operation_event(
            operation_id,
            event_type="cancelled",
            occurred_at=now,
            data={"reason": reason},
        )
        return operation

    def record_progress(
        self,
        operation_id: str,
        *,
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        if progress < 0 or (total is not None and (total < 0 or progress > total)):
            raise ValueError("Invalid operation progress")
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

    def recover_stale(self, *, updated_before: str) -> tuple[OperationRecord, ...]:
        recovered: list[OperationRecord] = []
        for operation in self._repository.list_stale_operations(
            status="RUNNING", updated_before=updated_before
        ):
            if operation.classification == "read":
                status = "FAILED"
                code = "INTERRUPTED_READ"
                retryable = True
            else:
                status = "UNKNOWN"
                code = "INTERRUPTED_WRITE_OUTCOME_UNKNOWN"
                retryable = False
            recovered.append(
                self._repository.transition_operation(
                    operation.operation_id,
                    expected_statuses=("RUNNING",),
                    status=status,
                    updated_at=self._clock(),
                    error_code=code,
                    error_message="Adapter stopped while the operation was running",
                    retryable=retryable,
                )
            )
        return tuple(recovered)

    def _requested_schema(self, operation_id: str) -> dict[str, Any]:
        operation = self._repository.get_operation(operation_id)
        if operation.status != "INPUT_REQUIRED":
            raise InvalidStateError(f"Operation {operation_id} is not waiting for input")
        for event in reversed(self._repository.list_operation_events(operation_id)):
            if event.event_type == "input_requested":
                schema = thaw_json(event.data.get("schema"))
                if isinstance(schema, dict):
                    return cast(dict[str, Any], schema)
        raise InvalidStateError(f"Operation {operation_id} has no pending input request")
