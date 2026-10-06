# pyright: basic
"""Resources, templates and prompts, plus the protocol-level items with no REST counterpart."""

import base64
from typing import Any
from urllib.parse import quote

import pytest
from mcp import types

from tests.validation.conftest import Recorder
from tests.validation.env import AtlasHandle, ConnectorHandle
from tests.validation.parity import (
    Verdict,
    connector_blocks,
    connector_resource,
    direct_blocks,
    direct_resource,
)

pytestmark = pytest.mark.validation


def _verdict(identical: bool, otherwise: Verdict = Verdict.UNSUPPORTED) -> Verdict:
    return Verdict.SAME if identical else otherwise


def _translated(carried: bool) -> Verdict:
    return Verdict.TRANSLATED if carried else Verdict.UNSUPPORTED


def _direct_read(atlas: AtlasHandle, uri: str) -> list[dict[str, Any]]:
    return [direct_resource(item) for item in atlas.direct().read(uri).contents]


def _via_read(
    conn: ConnectorHandle, operation: str, arguments: dict[str, Any] | None = None
) -> Any:
    response = conn.call(operation, arguments)
    if response.status_code != 200:
        return response
    return [connector_resource(item) for item in response.json()["contents"]]


# --- resources ----------------------------------------------------------------------------


def test_resources_list_every_resource_is_reachable_with_its_uri(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    resources = atlas.direct().snapshot()["resources"]
    operations = conn.operations()
    missing = [r.name for r in resources if r.name not in operations]
    undescribed = [
        r.name
        for r in resources
        if r.name in operations and str(r.uri) not in operations[r.name]["description"]
    ]
    record(
        "resources/list",
        "resources",
        _translated(not missing and not undescribed),
        f"{len(resources)} resources with URI, name, type",
        f"{len(resources) - len(missing)} operations naming their URI",
        "There is no list call: each resource is an operation whose description states its URI",
        standard="resources-list",
    )
    assert not missing and not undescribed


@pytest.mark.parametrize(
    ("scenario", "name", "uri", "standard"),
    [
        ("resources/read: text", "handbook", "kb://handbook", "resources-read-text"),
        ("resources/read: plain text", "faq", "kb://faq", None),
        ("resources/read: json", "config", "kb://config.json", None),
        ("resources/read: binary (image)", "logo", "kb://logo.png", "resources-read-binary"),
        ("resources/read: binary (opaque)", "archive", "kb://archive.bin", "resources-read-binary"),
    ],
)
def test_resources_read_is_identical(
    atlas: AtlasHandle,
    conn: ConnectorHandle,
    record: Recorder,
    scenario: str,
    name: str,
    uri: str,
    standard: str | None,
) -> None:
    direct = _direct_read(atlas, uri)
    via = _via_read(conn, name)
    identical = via == direct
    record(
        scenario,
        "resources",
        _verdict(identical),
        direct[0].get("mime"),
        via[0].get("mime") if isinstance(via, list) else f"HTTP {via.status_code}",
        standard=standard,
    )
    assert identical


def test_resources_read_a_resource_above_the_limit_is_refused_by_policy(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    assert len(_direct_read(atlas, "kb://big-text")[0]["text"]) >= 3_000_000
    response = conn.call("big_text")
    record(
        "resources/read: larger than the limit",
        "resources",
        Verdict.POLICY,
        "3 MB returned",
        f"HTTP {response.status_code} {response.json()['error']}",
        "The owner's maxBytes (default 256 KB) applies",
    )
    assert response.status_code == 502 and response.json()["error"] == "DOWNSTREAM_CONTENT_REJECTED"


def test_resources_read_always_returns_current_content(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    first, second = conn.call("clock").json(), conn.call("clock").json()
    changed = first["contents"][0]["text"] != second["contents"][0]["text"]
    record(
        "resources/read: changing content",
        "resources",
        _verdict(changed),
        "new value every read",
        f"{first['contents'][0]['text']} then {second['contents'][0]['text']}",
    )
    assert changed


def test_resources_templates_list_and_read(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    templates = atlas.direct().snapshot()["templates"]
    operations = conn.operations()
    fields: dict[str, set[str]] = {
        t.name: set(
            operations[t.name]["requestBody"]["content"]["application/json"]["schema"]["properties"]
        )
        for t in templates
    }
    expected = {
        "article": {"slug"},
        "issue_thread": {"project", "issue_id"},
        "file": {"path"},
    }
    record(
        "resources/templates/list",
        "resources",
        _translated(fields == expected),
        "uriTemplate variables",
        f"request fields {fields}",
        "Each template is an operation whose body holds the template variables",
        standard="resources-templates-read",
    )
    assert fields == expected

    direct = _direct_read(atlas, "issues://core/ISSUE-1")
    via = _via_read(conn, "issue_thread", {"project": "core", "issue_id": "ISSUE-1"})
    record(
        "resources/read: template",
        "resources",
        _verdict(via == direct),
        direct[0]["text"],
        via[0]["text"] if isinstance(via, list) else via.status_code,
        standard="resources-templates-read",
    )
    assert via == direct


@pytest.mark.parametrize(
    ("value", "refused"),
    [
        ("plain", False),
        ("with space", False),
        ("unicode-é", False),
        ("a/b", False),
        ("../x", False),
        ("%2e%2e", False),
        ("..", True),
        ("x\x00y", True),
    ],
)
def test_resources_template_values_never_escape_their_slot(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder, value: str, refused: bool
) -> None:
    response = conn.call("file", {"path": value})
    assert response.status_code < 500
    if refused:
        record(
            f"resources/read: template value {value!r}",
            "resources",
            Verdict.POLICY,
            "server would receive it",
            f"HTTP {response.status_code}",
            "Dot segments and control characters are never put into a URI",
        )
        assert response.status_code == 400
        return
    # What an MCP client sends for the same value: one percent-encoded path segment.
    direct = atlas.direct().read(f"files://{quote(value, safe='')}").contents[0]
    assert isinstance(direct, types.TextResourceContents)
    via = response.json()["contents"][0]["text"]
    record(
        f"resources/read: template value {value!r}",
        "resources",
        _verdict(via == direct.text),
        direct.text,
        via,
    )
    assert via == direct.text and "/../" not in via


def test_resources_subscriptions_are_not_available_through_rest(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    capabilities = atlas.direct().snapshot()["server"].capabilities
    subscribe = bool(capabilities.resources and capabilities.resources.subscribe)
    record(
        "resources/subscribe, list_changed",
        "protocol",
        Verdict.NOT_APPLICABLE,
        f"subscribe={subscribe}",
        "no push channel",
        "A request/response API has no way to push; re-read the operation instead",
        standard="resources-subscribe",
    )


# --- prompts ------------------------------------------------------------------------------


def _direct_messages(atlas: AtlasHandle, name: str, arguments: dict[str, str]) -> list[Any]:
    return [
        (message.role, direct_blocks([message.content]))
        for message in atlas.direct().prompt(name, arguments).messages
    ]


def _via_messages(conn: ConnectorHandle, name: str, arguments: dict[str, str]) -> Any:
    response = conn.call(name, arguments)
    if response.status_code != 200:
        return response
    return [
        (message["role"], connector_blocks(message["content"]))
        for message in response.json()["messages"]
    ]


def test_prompts_list_arguments_and_requirements(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    prompts = atlas.direct().snapshot()["prompts"]
    mismatched: list[str] = []
    for prompt in prompts:
        schema = conn.operations()[prompt.name]["requestBody"]["content"]["application/json"][
            "schema"
        ]
        arguments = prompt.arguments or []
        if set(schema["properties"]) != {a.name for a in arguments} or set(schema["required"]) != {
            a.name for a in arguments if a.required
        }:
            mismatched.append(prompt.name)
    record(
        "prompts/list",
        "prompts",
        _verdict(not mismatched),
        f"{len(prompts)} prompts with arguments",
        f"{len(mismatched)} differ",
        standard="prompts-list",
    )
    assert mismatched == []


@pytest.mark.parametrize(
    ("scenario", "name", "arguments", "standard"),
    [
        ("prompts/get: simple", "no_arguments", {}, "prompts-get-simple"),
        (
            "prompts/get: with arguments",
            "triage",
            {"issue_id": "ISSUE-1", "tone": "urgent"},
            "prompts-get-with-args",
        ),
        (
            "prompts/get: default argument",
            "triage",
            {"issue_id": "ISSUE-1"},
            "prompts-get-with-args",
        ),
        (
            "prompts/get: roles and embedded resource",
            "release_notes",
            {"version": "2.0"},
            "prompts-get-embedded-resource",
        ),
        ("prompts/get: unicode", "greeting", {"name": "Ada"}, None),
    ],
)
def test_prompts_get_is_identical(
    atlas: AtlasHandle,
    conn: ConnectorHandle,
    record: Recorder,
    scenario: str,
    name: str,
    arguments: dict[str, str],
    standard: str | None,
) -> None:
    direct = _direct_messages(atlas, name, arguments)
    via = _via_messages(conn, name, arguments)
    identical = via == direct
    record(
        scenario,
        "prompts",
        _verdict(identical),
        [role for role, _ in direct],
        [role for role, _ in via] if isinstance(via, list) else f"HTTP {via.status_code}",
        standard=standard,
    )
    assert identical


# --- protocol-level behaviour -------------------------------------------------------------


def test_protocol_level_scenarios_have_no_rest_counterpart(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    direct = atlas.direct()
    assert direct.ping()
    info = direct.snapshot()["server"]
    spec_info = conn.spec["info"]
    record(
        "initialize and capabilities",
        "protocol",
        Verdict.NOT_APPLICABLE,
        f"MCP {info.protocolVersion}, {info.serverInfo.name}",
        f"OpenAPI 3.1, '{spec_info['title']}'",
        "Capabilities are described by the API description instead",
        standard="server-initialize",
    )
    for scenario, standard, note in [
        ("ping", "ping", "Use GET /health"),
        ("logging/setLevel", "logging-set-level", "Server logs arrive with the operation's events"),
        ("completion/complete", "completion-complete", "Argument completion is not offered"),
        (
            "SSE streams and resumption",
            "sse-multiple-streams",
            "Transport detail of MCP, not of REST",
        ),
        ("session lifecycle", "session-lifecycle", "The adapter opens a short session per call"),
        ("DNS rebinding protection", "dns-rebinding", "Applies to the MCP server itself"),
    ]:
        record(
            scenario,
            "protocol",
            Verdict.NOT_APPLICABLE,
            "MCP transport behaviour",
            "REST has no equivalent",
            note,
            standard=standard,
        )


def test_binary_data_round_trips_byte_for_byte(atlas: AtlasHandle, conn: ConnectorHandle) -> None:
    direct = atlas.direct().read("kb://archive.bin").contents[0]
    assert isinstance(direct, types.BlobResourceContents)
    via = conn.call("archive").json()["contents"][0]
    assert base64.b64decode(via["data"]) == base64.b64decode(direct.blob) == bytes(range(256)) * 256
