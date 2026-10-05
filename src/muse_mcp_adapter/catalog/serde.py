from collections.abc import Mapping, Sequence
from typing import cast

from ..domain.errors import InvalidStateError
from ..domain.models import PublishedCatalog, PublishedPrimitive
from .canonical import digest, freeze_json, thaw_json


def catalog_to_dict(catalog: PublishedCatalog) -> dict[str, object]:
    return {
        "catalogVersion": catalog.catalog_version,
        "catalogDigest": catalog.catalog_digest,
        "server": thaw_json(catalog.server),
        "protocolVersion": catalog.protocol_version,
        "discoveredAt": catalog.discovered_at,
        "publishedAt": catalog.published_at,
        "primitives": [
            {
                "primitiveKey": item.primitive_key,
                "primitiveType": item.primitive_type,
                "mcpName": item.mcp_name,
                "publicOperationId": item.public_operation_id,
                "classification": item.classification,
                "definitionDigest": item.definition_digest,
                "contractDigest": item.contract_digest,
                "definition": thaw_json(item.definition),
                "policy": thaw_json(item.policy),
            }
            for item in catalog.primitives
        ],
    }


def catalog_from_dict(value: Mapping[str, object]) -> PublishedCatalog:
    primitive_values = value.get("primitives")
    if not _sequence(primitive_values):
        raise InvalidStateError("Stored catalog has invalid primitives")

    primitives: list[PublishedPrimitive] = []
    for raw in cast(Sequence[object], primitive_values):
        item = _mapping(raw, "Stored catalog primitive is invalid")
        definition = _mapping(item.get("definition"), "Stored primitive definition is invalid")
        policy = _mapping(item.get("policy"), "Stored primitive policy is invalid")
        primitives.append(
            PublishedPrimitive(
                primitive_key=_string(item, "primitiveKey"),
                primitive_type=_string(item, "primitiveType"),
                mcp_name=_string(item, "mcpName"),
                public_operation_id=_string(item, "publicOperationId"),
                classification=_string(item, "classification"),
                definition_digest=_string(item, "definitionDigest"),
                contract_digest=_string(item, "contractDigest"),
                definition=cast(Mapping[str, object], freeze_json(definition)),
                policy=cast(Mapping[str, object], freeze_json(policy)),
            )
        )

    server = _mapping(value.get("server"), "Stored catalog server is invalid")
    catalog = PublishedCatalog(
        catalog_version=_string(value, "catalogVersion"),
        catalog_digest=_string(value, "catalogDigest"),
        server=cast(Mapping[str, object], freeze_json(server)),
        protocol_version=_string(value, "protocolVersion"),
        discovered_at=_string(value, "discoveredAt"),
        published_at=_string(value, "publishedAt"),
        primitives=tuple(primitives),
    )
    _verify_digest(catalog)
    return catalog


def _verify_digest(catalog: PublishedCatalog) -> None:
    payload = catalog_to_dict(catalog)
    stored = cast(str, payload.pop("catalogDigest"))
    if digest(payload) != stored:
        raise InvalidStateError("Stored catalog digest does not match its content")


def _mapping(value: object, message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidStateError(message)
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise InvalidStateError(message)
    return cast(Mapping[str, object], mapping)


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise InvalidStateError(f"Stored catalog field {key} is invalid")
    return item


def _sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
