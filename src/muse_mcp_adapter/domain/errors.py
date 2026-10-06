class CatalogValidationError(ValueError):
    code = "CATALOG_VALIDATION_FAILED"

    def __init__(self, issues: list[str]) -> None:
        self.issues = tuple(issues)
        super().__init__(f"Catalog validation failed with {len(issues)} issue(s)")


class ControlPlaneError(RuntimeError):
    """Base class for expected administrative failures."""


class EntityNotFoundError(ControlPlaneError):
    pass


class ConflictError(ControlPlaneError):
    pass


class InvalidStateError(ControlPlaneError):
    pass


class DataPlaneError(RuntimeError):
    code = "DATA_PLANE_ERROR"


class OperationNotFoundError(DataPlaneError):
    code = "OPERATION_NOT_FOUND"


class UnsupportedPrimitiveError(DataPlaneError):
    code = "UNSUPPORTED_PRIMITIVE"


class RequestValidationError(DataPlaneError):
    code = "REQUEST_VALIDATION_FAILED"


class CredentialResolutionError(DataPlaneError):
    code = "CREDENTIAL_RESOLUTION_FAILED"


class DownstreamContentError(DataPlaneError):
    code = "DOWNSTREAM_CONTENT_REJECTED"


class ApprovalRequiredError(DataPlaneError):
    code = "APPROVAL_REQUIRED"

    def __init__(self, message: str, *, operation_id: str, action_digest: str) -> None:
        self.operation_id = operation_id
        self.action_digest = action_digest
        super().__init__(message)


class IdempotencyConflictError(DataPlaneError):
    code = "IDEMPOTENCY_CONFLICT"


class OperationInProgressError(DataPlaneError):
    code = "OPERATION_IN_PROGRESS"


class DownstreamToolError(DataPlaneError):
    code = "DOWNSTREAM_TOOL_ERROR"


class UnknownOutcomeError(DataPlaneError):
    code = "UNKNOWN_OUTCOME"

    def __init__(self, message: str, *, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(message)


class RateLimitExceededError(DataPlaneError):
    code = "RATE_LIMIT_EXCEEDED"


class UnsafeEndpointError(DataPlaneError):
    code = "UNSAFE_ENDPOINT"


class PublishedPolicyError(DataPlaneError):
    code = "PUBLISHED_POLICY_INVALID"


class AuthenticationError(DataPlaneError):
    code = "AUTHENTICATION_FAILED"


class AuthorizationError(DataPlaneError):
    code = "AUTHORIZATION_FAILED"


class ServerDisabledError(DataPlaneError):
    code = "SERVER_DISABLED"


class OperationCancelledError(DataPlaneError):
    code = "OPERATION_CANCELLED"


class InputRequiredError(DataPlaneError):
    code = "INPUT_REQUIRED"

    def __init__(
        self,
        message: str,
        *,
        operation_id: str,
        schema: object,
    ) -> None:
        self.operation_id = operation_id
        self.schema = schema
        super().__init__(message)


class RequestTooLargeError(DataPlaneError):
    code = "REQUEST_TOO_LARGE"


class ReauthorizationRequiredError(DataPlaneError):
    code = "REAUTHORIZATION_REQUIRED"

    def __init__(self, message: str, *, authorization_url: str | None = None) -> None:
        self.authorization_url = authorization_url
        super().__init__(message)
