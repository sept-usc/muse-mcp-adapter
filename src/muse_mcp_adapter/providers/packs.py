from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..catalog.canonical import freeze_json
from ..domain.errors import InvalidStateError, RequestValidationError


@dataclass(frozen=True, slots=True)
class ProviderPack:
    pack_id: str
    display_name: str
    documentation_url: str
    endpoint: str
    profiles: Mapping[str, str]
    auth_modes: tuple[str, ...]
    required_options: tuple[str, ...]
    allowed_options: tuple[str, ...]
    default_options: Mapping[str, str]
    safety: Mapping[str, object]
    conformance: Mapping[str, object]


def provider_pack_ids() -> tuple[str, ...]:
    return ("atlassian", "linear", "supabase")


def load_provider_pack(pack_id: str) -> ProviderPack:
    if pack_id not in provider_pack_ids():
        raise InvalidStateError(f"Unknown provider pack {pack_id}")
    resource = files("muse_mcp_adapter").joinpath("provider_packs", f"{pack_id}.json")
    loaded: object = json.loads(resource.read_text(encoding="utf-8"))
    data = _mapping(loaded, "provider pack")
    pack = ProviderPack(
        pack_id=_string(data, "id"),
        display_name=_string(data, "displayName"),
        documentation_url=_https_url(data, "documentationUrl"),
        endpoint=_https_url(data, "endpoint"),
        profiles=cast(Mapping[str, str], freeze_json(_string_mapping(data.get("profiles", {})))),
        auth_modes=_string_sequence(data, "authModes"),
        required_options=_string_sequence(data, "requiredOptions"),
        allowed_options=_string_sequence(data, "allowedOptions"),
        default_options=cast(
            Mapping[str, str],
            freeze_json(_string_mapping(data.get("defaultOptions", {}))),
        ),
        safety=cast(Mapping[str, object], freeze_json(_mapping(data.get("safety"), "safety"))),
        conformance=cast(
            Mapping[str, object],
            freeze_json(_mapping(data.get("conformance"), "conformance")),
        ),
    )
    _validate_pack(pack, expected_id=pack_id)
    return pack


def build_endpoint(
    pack: ProviderPack,
    *,
    profile: str = "default",
    options: Mapping[str, str] | None = None,
    allow_unsafe_overrides: bool = False,
) -> str:
    provided = dict(options or {})
    unknown = set(provided) - set(pack.allowed_options)
    if unknown:
        raise RequestValidationError("Unknown provider options: " + ", ".join(sorted(unknown)))
    values = dict(pack.default_options)
    values.update(provided)
    if (
        pack.safety.get("readOnlyByDefault") is True
        and values.get("read_only", "true").lower() != "true"
        and not allow_unsafe_overrides
    ):
        raise RequestValidationError(
            "Disabling the provider pack's read-only default requires an explicit unsafe override"
        )
    missing = [key for key in pack.required_options if not values.get(key)]
    if missing:
        raise RequestValidationError("Missing provider options: " + ", ".join(sorted(missing)))
    if profile == "default":
        endpoint = pack.endpoint
    else:
        endpoint = pack.profiles.get(profile)
        if endpoint is None:
            raise RequestValidationError(f"Unknown provider endpoint profile {profile}")
    parsed = urlsplit(endpoint)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    existing.update(values)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(existing), parsed.fragment)
    )


def validate_provider_registration(pack: ProviderPack, *, endpoint: str, auth_mode: str) -> None:
    if auth_mode not in pack.auth_modes:
        raise RequestValidationError(
            f"Authentication mode {auth_mode} is not supported by provider pack {pack.pack_id}"
        )
    parsed = urlsplit(endpoint)
    candidates = {"default": pack.endpoint, **dict(pack.profiles)}
    profile = next(
        (
            name
            for name, candidate in candidates.items()
            if (
                urlsplit(candidate).scheme,
                urlsplit(candidate).netloc,
                urlsplit(candidate).path,
            )
            == (parsed.scheme, parsed.netloc, parsed.path)
        ),
        None,
    )
    if profile is None:
        raise RequestValidationError(f"Endpoint does not match provider pack {pack.pack_id}")
    options = dict(parse_qsl(parsed.query, keep_blank_values=True))
    expected = build_endpoint(pack, profile=profile, options=options)
    if dict(parse_qsl(urlsplit(expected).query, keep_blank_values=True)) != options:
        raise RequestValidationError("Provider endpoint options are not canonical")


def _validate_pack(pack: ProviderPack, *, expected_id: str) -> None:
    issues: list[str] = []
    if pack.pack_id != expected_id:
        issues.append("pack id does not match its file name")
    if len(set(pack.auth_modes)) != len(pack.auth_modes) or not pack.auth_modes:
        issues.append("authModes must be unique and non-empty")
    if set(pack.required_options) - set(pack.allowed_options):
        issues.append("requiredOptions must be included in allowedOptions")
    if set(pack.default_options) - set(pack.allowed_options):
        issues.append("defaultOptions must be included in allowedOptions")
    if pack.safety.get("classificationMode") != "explicit_customer_policy":
        issues.append("provider packs must require explicit customer classification")
    required_primitives = pack.conformance.get("requiredPrimitiveTypes")
    if not _sequence(required_primitives) or not set(
        cast(Sequence[object], required_primitives)
    ).issubset({"tool", "resource", "resource_template", "prompt"}):
        issues.append("conformance.requiredPrimitiveTypes is invalid")
    if issues:
        raise InvalidStateError(f"Provider pack {expected_id} is invalid: " + "; ".join(issues))


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidStateError(f"Provider pack {label} must be an object")
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise InvalidStateError(f"Provider pack {label} has a non-string key")
    return cast(Mapping[str, object], mapping)


def _string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidStateError(f"Provider pack {key} must be a non-empty string")
    return value


def _https_url(data: Mapping[str, object], key: str) -> str:
    value = _string(data, key)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise InvalidStateError(f"Provider pack {key} must be an HTTPS URL")
    return value


def _string_sequence(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not _sequence(value) or not all(
        isinstance(item, str) and item for item in cast(Sequence[object], value)
    ):
        raise InvalidStateError(f"Provider pack {key} must be a string array")
    return tuple(cast(Sequence[str], value))


def _string_mapping(value: object) -> Mapping[str, str]:
    mapping = _mapping(value, "string mapping")
    if not all(isinstance(item, str) for item in mapping.values()):
        raise InvalidStateError("Provider pack mapping values must be strings")
    return cast(Mapping[str, str], mapping)


def _sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
