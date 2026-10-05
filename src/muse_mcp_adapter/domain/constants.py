from collections.abc import Mapping
from enum import StrEnum


class PrimitiveType(StrEnum):
    TOOL = "tool"
    RESOURCE = "resource"
    RESOURCE_TEMPLATE = "resource_template"
    PROMPT = "prompt"


class MuseClassification(StrEnum):
    READ = "read"
    WRITE = "write"
    SENSITIVE_WRITE = "sensitive_write"


def primitive_key(primitive: Mapping[str, object]) -> str:
    return f"{primitive.get('type')}:{primitive.get('name')}"
