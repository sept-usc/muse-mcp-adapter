from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from urllib.parse import urlsplit

from ..catalog.canonical import canonicalize, digest, freeze_json
from ..domain.constants import primitive_key
from ..domain.errors import CatalogValidationError
from ..domain.models import PublishedCatalog, PublishedPrimitive
from ..policy.validate_policy import (
    normalize_policy,
    validate_exposure_policy,
    validate_primitive,
)


def compile_catalog(
    *,
    catalog_version: str,
    server: Mapping[str, object],
    protocol_version: str,
    discovered_at: str,
    published_at: str,
    primitives: Sequence[Mapping[str, object]],
    policies: Sequence[Mapping[str, object]],
) -> PublishedCatalog:
    issues: list[str] = []
    _validate_header(
        catalog_version=catalog_version,
        server=server,
        protocol_version=protocol_version,
        discovered_at=discovered_at,
        published_at=published_at,
        issues=issues,
    )

    primitives_by_key = _index_unique(primitives, primitive_key, "primitive", issues)
    policies_by_key = _index_unique(
        policies,
        lambda policy: policy.get("primitiveKey"),
        "policy",
        issues,
    )

    for key, primitive in primitives_by_key.items():
        issues.extend(f"{key}: {issue}" for issue in validate_primitive(primitive))

    exposed: list[PublishedPrimitive] = []
    public_operation_ids: set[str] = set()

    for policy in policies:
        if policy.get("allowed") is not True:
            continue

        key = policy.get("primitiveKey")
        primitive = primitives_by_key.get(key) if isinstance(key, str) else None
        if primitive is None:
            issues.append(f"Policy references unknown primitive {key}")
            continue

        policy = normalize_policy(primitive, policy)
        issues.extend(validate_exposure_policy(primitive, policy))
        operation_id = policy.get("publicOperationId")
        if isinstance(operation_id, str):
            if operation_id in public_operation_ids:
                issues.append(f"Duplicate publicOperationId: {operation_id}")
            public_operation_ids.add(operation_id)

        definition = canonicalize(primitive)
        normalized_policy = canonicalize(policy)
        definition_digest = digest(definition)
        exposed.append(
            PublishedPrimitive(
                primitive_key=primitive_key(primitive),
                primitive_type=str(primitive.get("type")),
                mcp_name=str(primitive.get("name")),
                public_operation_id=str(operation_id),
                classification=str(policy.get("classification")),
                definition_digest=definition_digest,
                contract_digest=digest(
                    {"definitionDigest": definition_digest, "policy": normalized_policy}
                ),
                definition=freeze_json(definition),  # type: ignore[arg-type]
                policy=freeze_json(normalized_policy),  # type: ignore[arg-type]
            )
        )

    for key, policy in policies_by_key.items():
        if key not in primitives_by_key and policy.get("allowed") is not True:
            issues.append(f"Policy references unknown primitive {key}")

    if issues:
        raise CatalogValidationError(issues)

    exposed.sort(key=lambda item: item.public_operation_id)
    catalog_payload = {
        "catalogVersion": catalog_version,
        "server": server,
        "protocolVersion": protocol_version,
        "discoveredAt": discovered_at,
        "publishedAt": published_at,
        "primitives": [
            {
                "primitiveKey": item.primitive_key,
                "primitiveType": item.primitive_type,
                "mcpName": item.mcp_name,
                "publicOperationId": item.public_operation_id,
                "classification": item.classification,
                "definitionDigest": item.definition_digest,
                "contractDigest": item.contract_digest,
                "definition": item.definition,
                "policy": item.policy,
            }
            for item in exposed
        ],
    }

    return PublishedCatalog(
        catalog_version=catalog_version,
        catalog_digest=digest(catalog_payload),
        server=freeze_json(server),  # type: ignore[arg-type]
        protocol_version=protocol_version,
        discovered_at=discovered_at,
        published_at=published_at,
        primitives=tuple(exposed),
    )


def _validate_header(
    *,
    catalog_version: str,
    server: Mapping[str, object],
    protocol_version: str,
    discovered_at: str,
    published_at: str,
    issues: list[str],
) -> None:
    if not catalog_version:
        issues.append("catalogVersion is required")
    if not server.get("id"):
        issues.append("server.id is required")
    if not _http_url(server.get("endpoint")):
        issues.append("server.endpoint must be an HTTP or HTTPS URL")
    if not protocol_version:
        issues.append("protocolVersion is required")
    if not _iso_datetime(discovered_at):
        issues.append("discoveredAt must be an ISO date-time")
    if not _iso_datetime(published_at):
        issues.append("publishedAt must be an ISO date-time")


def _index_unique(
    items: Sequence[Mapping[str, object]],
    key_of: Callable[[Mapping[str, object]], object],
    label: str,
    issues: list[str],
) -> dict[str, Mapping[str, object]]:
    index: dict[str, Mapping[str, object]] = {}
    for item in items:
        key = key_of(item)
        if not isinstance(key, str) or not key:
            issues.append(f"{label} has no stable key")
            continue
        if key in index:
            issues.append(f"Duplicate {label}: {key}")
            continue
        index[key] = item
    return index


def _http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _iso_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None
