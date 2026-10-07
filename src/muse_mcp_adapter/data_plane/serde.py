from collections.abc import Mapping, Sequence
from typing import cast

from ..catalog.canonical import freeze_json, thaw_json
from ..domain.errors import InvalidStateError
from ..domain.models import (
    DataPlaneResponse,
    NormalizedContent,
    NormalizedMessage,
)

REPLAY_NOTE = (
    "This repeats an identical request made a moment ago, so nothing new was done and the "
    "earlier result is returned. To deliberately run it again, send a new unique "
    "Idempotency-Key."
)


def response_to_dict(response: DataPlaneResponse) -> dict[str, object]:
    body = _response_fields(response)
    if response.replayed:
        body["idempotentReplay"] = True
        body["note"] = REPLAY_NOTE
    return body


def _response_fields(response: DataPlaneResponse) -> dict[str, object]:
    return {
        "requestId": response.request_id,
        "catalogVersion": response.catalog_version,
        "publicOperationId": response.public_operation_id,
        "primitiveType": response.primitive_type,
        "provenance": thaw_json(response.provenance),
        "operationId": response.operation_id,
        "operationStatus": response.operation_status,
        "structuredContent": (
            thaw_json(response.structured_content)
            if response.structured_content is not None
            else None
        ),
        "contents": [_content_to_dict(item) for item in response.contents],
        "messages": [
            {
                "role": message.role,
                "content": [_content_to_dict(item) for item in message.content],
            }
            for message in response.messages
        ],
    }


def response_from_dict(value: Mapping[str, object]) -> DataPlaneResponse:
    provenance = _mapping(value.get("provenance"), "Stored response provenance is invalid")
    structured_value = value.get("structuredContent")
    structured = (
        cast(
            Mapping[str, object],
            freeze_json(_mapping(structured_value, "Stored structured content is invalid")),
        )
        if structured_value is not None
        else None
    )
    contents = tuple(
        _content_from_dict(_mapping(item, "Stored response content is invalid"))
        for item in _sequence(value.get("contents"), "Stored response contents are invalid")
    )
    messages: list[NormalizedMessage] = []
    for raw in _sequence(value.get("messages"), "Stored response messages are invalid"):
        message = _mapping(raw, "Stored response message is invalid")
        messages.append(
            NormalizedMessage(
                role=_string(message, "role"),
                content=tuple(
                    _content_from_dict(_mapping(item, "Stored message content is invalid"))
                    for item in _sequence(
                        message.get("content"), "Stored message content is invalid"
                    )
                ),
            )
        )

    return DataPlaneResponse(
        request_id=_string(value, "requestId"),
        catalog_version=_string(value, "catalogVersion"),
        public_operation_id=_string(value, "publicOperationId"),
        primitive_type=_string(value, "primitiveType"),
        provenance=cast(Mapping[str, object], freeze_json(provenance)),
        operation_id=_optional_string(value.get("operationId")),
        operation_status=_optional_string(value.get("operationStatus")),
        structured_content=structured,
        contents=contents,
        messages=tuple(messages),
    )


def _content_to_dict(content: NormalizedContent) -> dict[str, object]:
    return {
        "kind": content.kind,
        "text": content.text,
        "mimeType": content.mime_type or None,
        "uri": content.uri,
        "annotations": thaw_json(content.annotations),
        "data": content.data,
        "name": content.name,
    }


def _content_from_dict(value: Mapping[str, object]) -> NormalizedContent:
    annotations = _mapping(value.get("annotations"), "Stored annotations are invalid")
    return NormalizedContent(
        kind=_string(value, "kind"),
        text=_string(value, "text"),
        mime_type=_optional_string(value.get("mimeType")) or "",
        uri=_optional_string(value.get("uri")),
        annotations=cast(Mapping[str, object], freeze_json(annotations)),
        data=_optional_string(value.get("data")),
        name=_optional_string(value.get("name")),
    )


def _mapping(value: object, message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidStateError(message)
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise InvalidStateError(message)
    return cast(Mapping[str, object], mapping)


def _sequence(value: object, message: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise InvalidStateError(message)
    return cast(Sequence[object], value)


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise InvalidStateError(f"Stored response field {key} is invalid")
    return item


def _optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise InvalidStateError("Stored optional string is invalid")
