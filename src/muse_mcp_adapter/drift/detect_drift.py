from collections.abc import Mapping, Sequence

from ..catalog.canonical import canonical_json, digest
from ..domain.constants import primitive_key
from ..domain.models import DriftEvent, PublishedCatalog


def detect_drift(
    published_catalog: PublishedCatalog,
    discovered_primitives: Sequence[Mapping[str, object]],
) -> tuple[DriftEvent, ...]:
    events: list[DriftEvent] = []
    published = {item.primitive_key: item for item in published_catalog.primitives}
    discovered = {primitive_key(item): item for item in discovered_primitives}

    for key, primitive in discovered.items():
        previous = published.get(key)
        if previous is None:
            events.append(_event(key, "capability", "review", "primitive_added"))
            continue

        if digest(primitive) == previous.definition_digest:
            continue

        checks = (
            ("inputSchema", "schema", "block", "input_schema_changed"),
            ("outputSchema", "schema", "block", "output_schema_changed"),
            ("annotations", "permission", "block", "annotations_changed"),
            ("requiredScopes", "authorization", "block", "required_scopes_changed"),
            ("resultContentTypes", "result", "block", "result_content_changed"),
            ("lifecycleModes", "lifecycle", "block", "lifecycle_changed"),
        )

        classified = False
        for field, drift_type, severity, reason in checks:
            if not _equal(previous.definition.get(field), primitive.get(field)):
                events.append(_event(key, drift_type, severity, reason))
                classified = True

        if not classified:
            events.append(_event(key, "schema", "block", "definition_changed"))

    for key in published:
        if key not in discovered:
            events.append(_event(key, "capability", "block", "primitive_removed"))

    return tuple(sorted(events, key=lambda item: (item.primitive_key, item.reason)))


def _event(primitive_key_value: str, drift_type: str, severity: str, reason: str) -> DriftEvent:
    return DriftEvent(
        primitive_key=primitive_key_value,
        drift_type=drift_type,
        severity=severity,
        reason=reason,
    )


def _equal(left: object, right: object) -> bool:
    return canonical_json(left) == canonical_json(right)
