# pyright: basic
"""Tools: what an MCP client gets directly versus what the connector returns."""

from typing import Any

import pytest
from mcp import types

from tests.validation.conftest import Recorder
from tests.validation.env import AtlasHandle, ConnectorHandle
from tests.validation.parity import Verdict, connector_blocks, direct_blocks, expand_refs

pytestmark = pytest.mark.validation
AREA = "tools"


def _verdict(identical: bool, otherwise: Verdict = Verdict.UNSUPPORTED) -> Verdict:
    return Verdict.SAME if identical else otherwise


def _translated(carried: bool) -> Verdict:
    """The same information arrives through the REST equivalent of the MCP feature."""
    return Verdict.TRANSLATED if carried else Verdict.UNSUPPORTED


def test_tools_list_covers_every_tool(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    names = sorted(tool.name for tool in atlas.direct().list_tools())
    exposed = sorted(name for name in names if name in conn.operations())
    record(
        "tools/list: names",
        AREA,
        _verdict(exposed == names, Verdict.POLICY),
        f"{len(names)} tools",
        f"{len(exposed)} operations",
        standard="tools-list",
    )
    assert exposed == names


def test_tools_list_input_schemas_are_identical(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    components = conn.spec["components"]["schemas"]
    different: list[str] = []
    for tool in atlas.direct().list_tools():
        direct = expand_refs(tool.inputSchema, tool.inputSchema.get("$defs", {}), "#/$defs/")
        body = conn.operations()[tool.name]["requestBody"]["content"]["application/json"]["schema"]
        if direct != expand_refs(body, components, "#/components/schemas/"):
            different.append(tool.name)
    record(
        "tools/list: input schemas",
        AREA,
        _verdict(not different),
        "every tool's inputSchema",
        f"{len(different)} differ: {different}",
        standard="json-schema-2020-12",
    )
    assert different == []


def test_tools_list_annotations_become_classifications(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    wrong = [
        tool.name
        for tool in atlas.direct().list_tools()
        if (conn.operations()[tool.name]["x-muse-classification"] == "read")
        != bool(tool.annotations and tool.annotations.readOnlyHint)
    ]
    record(
        "tools/list: annotations",
        AREA,
        Verdict.TRANSLATED,
        "readOnlyHint, destructiveHint",
        "x-muse-classification + confirmation hint",
        "MCP hints are advisory; the owner's policy decides",
    )
    assert wrong == []


def test_tools_list_descriptions_are_preserved(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    missing = [
        tool.name
        for tool in atlas.direct().list_tools()
        if tool.description
        and tool.description.strip().splitlines()[0]
        not in conn.operations()[tool.name]["description"]
    ]
    record(
        "tools/list: descriptions",
        AREA,
        _translated(not missing),
        "tool description",
        "description plus a classification note",
    )
    assert missing == []


def test_tools_list_output_schemas_are_exposed(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    components = conn.spec["components"]["schemas"]
    declared = [tool for tool in atlas.direct().list_tools() if tool.outputSchema]
    matching = 0
    for tool in declared:
        schema = conn.operations()[tool.name]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        exposed = next(
            (
                part["properties"]["structuredContent"]
                for part in schema.get("allOf", [])
                if "properties" in part
            ),
            None,
        )
        assert tool.outputSchema is not None
        expected = expand_refs(tool.outputSchema, tool.outputSchema.get("$defs", {}), "#/$defs/")
        if (
            exposed is not None
            and expand_refs(exposed, components, "#/components/schemas/") == expected
        ):
            matching += 1
    record(
        "tools/list: output schemas",
        AREA,
        _verdict(matching == len(declared)),
        f"{len(declared)} tools declare an outputSchema",
        f"{matching} passed on",
        standard="tools-list",
    )
    assert matching == len(declared) > 0


def test_call_simple_text_and_structured_result(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    direct = atlas.direct().call("get_issue", {"issue_id": "ISSUE-1"})
    via = conn.call("get_issue", {"issue_id": "ISSUE-1"}).json()
    same = direct.structuredContent == via["structuredContent"] and direct_blocks(
        direct.content
    ) == connector_blocks(via["contents"])
    record(
        "tools/call: text + structured",
        AREA,
        _verdict(same),
        direct.structuredContent,
        via["structuredContent"],
        standard="tools-call-simple-text",
    )
    assert same


def test_call_nested_arguments_and_a_list_result(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    arguments = {
        "query": "export",
        "filters": {"tags": ["perf"], "since": "2026-01-31"},
        "limit": 1,
    }
    direct = atlas.direct().call("search_knowledge", arguments)
    via = conn.call("search_knowledge", arguments).json()
    same = direct.structuredContent == via["structuredContent"]
    record(
        "tools/call: nested arguments, list result",
        AREA,
        _verdict(same),
        direct.structuredContent,
        via["structuredContent"],
    )
    assert same


@pytest.mark.parametrize(
    ("scenario", "tool", "arguments", "standard"),
    [
        ("tools/call: image and audio", "render_chart", {}, "tools-call-image"),
        ("tools/call: embedded resource", "monthly_report", {}, "tools-call-embedded-resource"),
        ("tools/call: resource links", "related_documents", {"issue_id": "ISSUE-1"}, None),
        ("tools/call: unicode", "echo_unicode", {"text": "héllo 👋 שלום مرحبا ‮ end"}, None),
    ],
)
def test_call_content_types_are_carried_unchanged(
    atlas: AtlasHandle,
    conn: ConnectorHandle,
    record: Recorder,
    scenario: str,
    tool: str,
    arguments: dict[str, Any],
    standard: str | None,
) -> None:
    direct = direct_blocks(atlas.direct().call(tool, arguments).content)
    response = conn.call(tool, arguments)
    via = connector_blocks(response.json()["contents"]) if response.status_code == 200 else None
    record(
        scenario,
        AREA,
        _verdict(via == direct),
        [block["type"] for block in direct],
        [block["type"] for block in via] if via else f"HTTP {response.status_code}",
        standard=standard,
    )
    assert via == direct


def test_call_content_order_is_preserved(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    direct = [b["type"] for b in direct_blocks(atlas.direct().call("render_chart", {}).content)]
    via = [b["type"] for b in connector_blocks(conn.call("render_chart", {}).json()["contents"])]
    record(
        "tools/call: mixed content order",
        AREA,
        _verdict(via == direct),
        direct,
        via,
        standard="tools-call-mixed-content",
    )
    assert via == direct == ["text", "image", "audio"]


def test_call_error_is_reported_as_an_error_with_the_same_message(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    direct = atlas.direct().call("get_issue", {"issue_id": "ISSUE-999"})
    response = conn.call("get_issue", {"issue_id": "ISSUE-999"})
    assert direct.isError and response.status_code == 502
    carried = "ISSUE-999 was not found" in response.json()["message"]
    record(
        "tools/call: tool error",
        AREA,
        _translated(carried),
        "isError=true with the message",
        f"HTTP 502 {response.json()['error']} with the message",
        "A tool error becomes an HTTP error",
        standard="tools-call-error-handling",
    )
    assert carried


def test_call_invalid_arguments_are_rejected_before_the_server(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    direct = atlas.direct().call("get_issue", {})
    before = atlas.state.calls["get_issue"]
    response = conn.call("get_issue", {})
    record(
        "tools/call: invalid arguments",
        AREA,
        Verdict.TRANSLATED,
        "isError=true (the server validates)",
        f"HTTP {response.status_code} (the adapter validates)",
        "Bad input never reaches the server",
    )
    assert direct.isError and response.status_code == 400
    assert atlas.state.calls["get_issue"] == before


def test_call_unknown_tool(atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder) -> None:
    direct = atlas.direct().call("no_such_tool", {})
    response = conn.call("no_such_tool", {})
    record(
        "tools/call: unknown tool",
        AREA,
        Verdict.TRANSLATED,
        "isError=true",
        f"HTTP {response.status_code}",
    )
    assert direct.isError and response.status_code == 404


def test_call_progress_notifications(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    client = atlas.direct()
    client.call("progress_task", {"steps": 3})
    body = conn.call("progress_task", {"steps": 3}).json()
    events = conn.status(body["operationId"]).json()["events"]
    seen = [event["data"]["progress"] for event in events if event["type"] == "progress"]
    expected = [progress for progress, _, _ in client.progress]
    record(
        "tools/call: progress",
        AREA,
        _translated(seen == expected),
        expected,
        seen or "not visible to the caller",
        "Delivered with the operation's status, not as a live stream",
        standard="tools-call-with-progress",
    )
    assert seen == expected


def test_call_log_notifications(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    client = atlas.direct()
    client.call("progress_task", {"steps": 2})
    body = conn.call("progress_task", {"steps": 2}).json()
    events = conn.status(body["operationId"]).json()["events"]
    logged = [event["data"]["message"] for event in events if event["type"] == "log"]
    expected = [message for _, message in client.logs]
    record(
        "tools/call: logging",
        AREA,
        _translated(logged == expected),
        expected,
        logged or "not visible to the caller",
        "Delivered with the operation's status, not as a live stream",
        standard="tools-call-with-logging",
    )
    assert logged == expected


def test_call_sampling_and_roots_degrade_the_same_way(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    direct = atlas.direct().call("ask_client_model", {"prompt": "hi"})
    via = conn.call("ask_client_model", {"prompt": "hi"}).json()
    same = direct_blocks(direct.content) == connector_blocks(via["contents"])
    record(
        "tools/call: sampling unavailable",
        AREA,
        _verdict(same),
        direct_blocks(direct.content)[0]["text"],
        via["contents"][0]["text"],
        "Neither a plain MCP client nor the connector offers sampling",
        standard="tools-call-sampling",
    )
    assert same


def test_call_elicitation_is_translated_to_input_required_and_resume(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    direct = atlas.direct(elicit=lambda _message: {"time": "Tuesday 10am"})
    expected = direct_blocks(direct.call("schedule_review", {"issue_id": "ISSUE-1"}).content)

    asked = conn.call("schedule_review", {"issue_id": "ISSUE-1"})
    assert asked.status_code == 409 and asked.json()["error"] == "INPUT_REQUIRED"
    resumed = conn.http.post(
        f"/c/{conn.slug}/executions/{asked.json()['operationId']}/resume",
        json={"arguments": {"issue_id": "ISSUE-1"}, "inputs": {"time": "Tuesday 10am"}},
        headers=conn.headers,
    )
    via = connector_blocks(resumed.json()["contents"])
    record(
        "tools/call: elicitation",
        AREA,
        _translated(via == expected),
        "the server asks mid-call, the client answers",
        "409 INPUT_REQUIRED, then resume",
        "The question reaches the caller as a response, and the answer comes in a second call",
    )
    assert via == expected


def test_call_oversized_results_are_refused_by_policy(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    direct = atlas.direct().call("payload", {"size_kb": 512})
    response = conn.call("payload", {"size_kb": 512})
    assert not direct.isError and response.status_code == 502
    record(
        "tools/call: result above the limit",
        AREA,
        Verdict.POLICY,
        "512 KB returned",
        f"HTTP 502 {response.json()['error']}",
        "The owner's maxResultBytes (default 256 KB) applies",
    )
    assert response.json()["error"] == "DOWNSTREAM_CONTENT_REJECTED"


def test_call_structured_output_that_breaks_its_schema(
    atlas: AtlasHandle, conn: ConnectorHandle, record: Recorder
) -> None:
    direct = atlas.direct().call("bad_output", {"issue_id": "ISSUE-1"})
    response = conn.call("bad_output", {"issue_id": "ISSUE-1"})
    errored = bool(direct.isError) and response.status_code == 502
    record(
        "tools/call: structured output breaks its schema",
        AREA,
        Verdict.TRANSLATED,
        "isError=true",
        f"HTTP {response.status_code}",
    )
    assert errored
    assert not isinstance(direct.content[0], types.ImageContent)
