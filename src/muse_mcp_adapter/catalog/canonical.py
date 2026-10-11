import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import cast


def canonicalize(value: object, path: str = "$") -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"Non-finite number at {path}")
        return value

    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if not all(isinstance(key, str) for key in mapping):
            raise TypeError(f"Non-string object key at {path}")
        keys = cast(list[str], list(mapping.keys()))
        result: dict[str, object] = {}
        for key in sorted(keys):
            result[key] = canonicalize(mapping[key], f"{path}.{key}")
        return result

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return [
            canonicalize(item, f"{path}[{index}]") for index, item in enumerate(sequence)
        ]

    raise TypeError(f"Unsupported value at {path}")


def canonical_json(value: object) -> str:
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def digest(value: object) -> str:
    value_digest = hashlib.sha256(canonical_json(value).encode()).hexdigest()
    return f"sha256:{value_digest}"


def freeze_json(value: object) -> object:
    normalized = canonicalize(value)
    return _freeze(normalized)


def thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): thaw_json(item) for key, item in mapping.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return [thaw_json(item) for item in sequence]
    return value


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        return MappingProxyType({key: _freeze(item) for key, item in mapping.items()})
    if isinstance(value, list):
        sequence = cast(list[object], value)
        return tuple(_freeze(item) for item in sequence)
    return value
