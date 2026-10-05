import re
from collections.abc import Mapping, Sequence
from typing import cast
from urllib.parse import urlsplit

from ..domain.constants import MuseClassification, PrimitiveType, primitive_key

_OPERATION_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_URI_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*][^)]*\)[+*{]")

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 300
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_PATTERN_LENGTH = 256
FORBIDDEN_URI_SCHEMES = frozenset({"file", "data", "javascript", "vbscript", "ftp", "gopher"})


def normalize_policy(
    primitive: Mapping[str, object], policy: Mapping[str, object]
) -> dict[str, object]:
    """Fill optional policy fields with explicit defaults so runtime never sees gaps."""
    normalized: dict[str, object] = dict(policy)
    normalized.setdefault("requiredScopes", [])
    if primitive.get("type") == PrimitiveType.TOOL:
        normalized.setdefault("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)
    return normalized


def validate_primitive(primitive: Mapping[str, object]) -> list[str]:
    issues: list[str] = []
    primitive_type = primitive.get("type")

    if primitive_type not in PrimitiveType:
        issues.append(f"Unsupported primitive type: {primitive_type}")
    if not _non_empty_string(primitive.get("name")):
        issues.append("Primitive name is required")

    if primitive_type == PrimitiveType.TOOL and not _object_schema(primitive.get("inputSchema")):
        issues.append("Tool inputSchema must be a JSON Schema object")
    if primitive_type == PrimitiveType.TOOL:
        for field_name in ("inputSchema", "outputSchema"):
            issues.extend(_schema_issues(primitive.get(field_name), field_name))
    if primitive_type == PrimitiveType.RESOURCE and not _non_empty_string(primitive.get("uri")):
        issues.append("Resource uri is required")
    if primitive_type == PrimitiveType.RESOURCE_TEMPLATE and not _non_empty_string(
        primitive.get("uriTemplate")
    ):
        issues.append("Resource template uriTemplate is required")
    if primitive_type == PrimitiveType.PROMPT and not _sequence(primitive.get("arguments", [])):
        issues.append("Prompt arguments must be an array")
    if not _unique_strings(primitive.get("requiredScopes", [])):
        issues.append("requiredScopes must contain unique non-empty strings")

    return issues


def validate_exposure_policy(
    primitive: Mapping[str, object], policy: Mapping[str, object]
) -> list[str]:
    issues: list[str] = []
    key = primitive_key(primitive)

    if policy.get("primitiveKey") != key:
        issues.append(f"Policy primitiveKey must be {key}")
    if policy.get("allowed") is not True:
        issues.append(f"Published policy for {key} must set allowed to true")

    operation_id = policy.get("publicOperationId")
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        issues.append(f"Invalid publicOperationId for {key}")

    classification = policy.get("classification")
    if classification not in MuseClassification:
        issues.append(f"Invalid Muse classification for {key}")
    if primitive.get("type") != PrimitiveType.TOOL and classification != MuseClassification.READ:
        issues.append(f"{primitive.get('type')} {primitive.get('name')} must be classified as read")

    policy_scopes = _string_list(policy.get("requiredScopes", []))
    if policy_scopes is None or len(policy_scopes) != len(set(policy_scopes)):
        issues.append(f"Policy scopes for {key} must be unique non-empty strings")
    else:
        granted = set(policy_scopes)
        primitive_scopes = _string_list(primitive.get("requiredScopes", [])) or []
        for scope in primitive_scopes:
            if scope not in granted:
                issues.append(f"Policy for {key} is missing required scope {scope}")

    timeout = policy.get("timeoutSeconds")
    if timeout is not None and (
        not _is_int(timeout) or not 1 <= cast(int, timeout) <= MAX_TIMEOUT_SECONDS
    ):
        issues.append(f"timeoutSeconds for {key} must be an integer from 1 to 300")

    if primitive.get("type") == PrimitiveType.TOOL and classification == MuseClassification.READ:
        annotations = _string_mapping(primitive.get("annotations")) or {}
        if (
            annotations.get("readOnlyHint") is not True
            and policy.get("readOnlyReviewed") is not True
        ):
            issues.append(
                f"Tool {key} lacks readOnlyHint=true; classify it as write or set "
                "readOnlyReviewed after review"
            )

    if primitive.get("type") in (PrimitiveType.RESOURCE, PrimitiveType.RESOURCE_TEMPLATE):
        resource = _string_mapping(policy.get("resource"))
        if resource is None:
            issues.append(f"Resource policy for {key} is required")
        else:
            if not _unique_strings(resource.get("allowedMimeTypes")):
                issues.append(f"Resource policy for {key} requires allowedMimeTypes")
            if not _bounded_int(resource.get("maxBytes"), MAX_PAYLOAD_BYTES):
                issues.append(
                    f"Resource policy for {key} requires maxBytes from 1 to {MAX_PAYLOAD_BYTES}"
                )
            schemes = resource.get("allowedUriSchemes")
            if schemes is not None and not _unique_strings(schemes):
                issues.append(f"Resource policy for {key} has invalid allowedUriSchemes")
            hosts = resource.get("allowedUriHosts")
            if hosts is not None and not _unique_strings(hosts):
                issues.append(f"Resource policy for {key} has invalid allowedUriHosts")
            issues.extend(_argument_pattern_issues(resource.get("argumentPatterns"), key))
            uri_value = primitive.get("uri") or primitive.get("uriTemplate")
            if isinstance(uri_value, str):
                allowed = _string_list(schemes) if schemes is not None else None
                issues.extend(_uri_scheme_issues(uri_value, allowed, key))
                parsed = urlsplit(uri_value.replace("{", "x").replace("}", "x"))
                if parsed.scheme in ("http", "https"):
                    allowed_hosts = _string_list(hosts) if hosts is not None else None
                    if not allowed_hosts:
                        issues.append(
                            f"HTTP resource policy for {key} requires allowedUriHosts"
                        )
                    elif parsed.hostname not in {item.lower() for item in allowed_hosts}:
                        issues.append(
                            f"Resource URI host {parsed.hostname} is not in allowedUriHosts "
                            f"for {key}"
                        )

    if primitive.get("type") == PrimitiveType.TOOL:
        if not _bounded_int(policy.get("maxResultBytes"), MAX_PAYLOAD_BYTES):
            issues.append(
                f"Tool policy for {key} requires maxResultBytes from 1 to {MAX_PAYLOAD_BYTES}"
            )
        content_types = policy.get("allowedResultContentTypes")
        if content_types is not None and not _unique_strings(content_types):
            issues.append(f"Tool policy for {key} has invalid allowedResultContentTypes")

    if primitive.get("type") == PrimitiveType.PROMPT:
        prompt = _string_mapping(policy.get("prompt"))
        roles = _string_list(prompt.get("allowedRoles")) if prompt is not None else None
        if roles is None or len(roles) != len(set(roles)) or any(
            role not in ("user", "assistant") for role in roles
        ):
            issues.append(f"Prompt policy for {key} requires valid allowedRoles")
        embedded_mime_types = (
            _string_list(prompt.get("allowedEmbeddedMimeTypes"))
            if prompt is not None
            else None
        )
        if embedded_mime_types is None or len(embedded_mime_types) != len(
            set(embedded_mime_types)
        ):
            issues.append(f"Prompt policy for {key} requires allowedEmbeddedMimeTypes")
        max_bytes = prompt.get("maxBytes") if prompt is not None else None
        if not _bounded_int(max_bytes, MAX_PAYLOAD_BYTES):
            issues.append(
                f"Prompt policy for {key} requires maxBytes from 1 to {MAX_PAYLOAD_BYTES}"
            )
        if prompt is not None:
            issues.extend(_argument_pattern_issues(prompt.get("argumentPatterns"), key))

    return issues


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bounded_int(value: object, upper: int) -> bool:
    return _is_int(value) and 1 <= cast(int, value) <= upper


def _uri_scheme_issues(uri: str, allowed_schemes: list[str] | None, key: str) -> list[str]:
    match = _URI_SCHEME.match(uri)
    if match is None:
        return [f"Resource URI for {key} must start with a URI scheme"]
    scheme = match.group(1).lower()
    issues: list[str] = []
    if scheme in FORBIDDEN_URI_SCHEMES:
        issues.append(f"Resource URI scheme {scheme} is not permitted for {key}")
    if allowed_schemes is not None and scheme not in {item.lower() for item in allowed_schemes}:
        issues.append(f"Resource URI scheme {scheme} is not in allowedUriSchemes for {key}")
    if scheme in ("http", "https") and not urlsplit(uri.replace("{", "x").replace("}", "x")).netloc:
        issues.append(f"Resource URI for {key} has no host")
    return issues


def _argument_pattern_issues(value: object, key: str) -> list[str]:
    if value is None:
        return []
    mapping = _string_mapping(value)
    if mapping is None:
        return [f"argumentPatterns for {key} must be an object"]
    issues: list[str] = []
    for name, pattern in mapping.items():
        if not isinstance(pattern, str) or not pattern:
            issues.append(f"argumentPatterns.{name} for {key} must be a non-empty regex")
            continue
        issues.extend(_regex_issues(pattern, f"argumentPatterns.{name} for {key}"))
    return issues


def _regex_issues(pattern: str, label: str) -> list[str]:
    if len(pattern) > MAX_PATTERN_LENGTH:
        return [f"{label} exceeds {MAX_PATTERN_LENGTH} characters"]
    if _NESTED_QUANTIFIER.search(pattern):
        return [f"{label} contains a nested quantifier that risks catastrophic backtracking"]
    try:
        re.compile(pattern)
    except re.error:
        return [f"{label} is not a valid regular expression"]
    return []


def _schema_issues(schema: object, label: str) -> list[str]:
    """Reject downstream regexes that are oversized or prone to catastrophic backtracking."""
    issues: list[str] = []
    stack: list[object] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            mapping = cast(Mapping[object, object], node)
            for name, value in mapping.items():
                if name == "pattern" and isinstance(value, str):
                    issues.extend(_regex_issues(value, f"{label} pattern"))
                elif name == "patternProperties" and isinstance(value, Mapping):
                    for pattern in cast(Mapping[object, object], value):
                        if isinstance(pattern, str):
                            issues.extend(_regex_issues(pattern, f"{label} patternProperties"))
                stack.append(cast(object, value))
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
            stack.extend(cast(Sequence[object], node))
    return issues


def _object_schema(value: object) -> bool:
    mapping = _string_mapping(value)
    return mapping is not None and mapping.get("type") == "object"


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _unique_strings(value: object) -> bool:
    items = _string_list(value)
    if items is None:
        return False
    return len(items) == len(set(items))


def _string_list(value: object) -> list[str] | None:
    if not _sequence(value):
        return None
    items = cast(Sequence[object], value)
    if not all(_non_empty_string(item) for item in items):
        return None
    return cast(list[str], list(items))


def _string_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        return None
    return cast(Mapping[str, object], mapping)
