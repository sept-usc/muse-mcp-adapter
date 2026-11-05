"""The list of services the setup connector can look up by name."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

from muse_mcp_adapter.chat_setup import directory
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy

ROOT = Path(__file__).resolve().parents[2]
_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")


def _generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "generate_directory", ROOT / "scripts" / "generate_directory.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_listed_service_is_one_the_adapter_would_accept() -> None:
    policy = EndpointSecurityPolicy()
    seen_ids: set[str] = set()
    seen_addresses: set[str] = set()
    for entry in directory.ENTRIES:
        assert _ID.fullmatch(entry.id), entry.id
        assert entry.id not in seen_ids and entry.id != "adapter-setup"
        seen_ids.add(entry.id)
        address = entry.endpoint.rstrip("/").lower()
        assert address not in seen_addresses, f"{entry.id} repeats an address"
        seen_addresses.add(address)
        policy.validate(entry.endpoint, resolve=False)  # https, a host, no user, no fragment
        assert "." in entry.endpoint.split("/")[2] and not entry.endpoint.split("/")[2][0].isdigit()
        assert entry.credential in directory.CREDENTIALS
        assert entry.name and entry.summary
        assert entry.docs is None or entry.docs.startswith("https://")
        assert all(topic and topic == topic.strip() for topic in entry.topics)
        if entry.credential == "env_token":
            assert entry.secret_label and entry.token_help


def test_the_generated_list_adds_to_the_hand_written_one_without_overriding_it() -> None:
    assert [e.id for e in directory.ENTRIES[: len(directory.PRESETS)]] == [
        p.id for p in directory.PRESETS
    ]
    assert len(directory.ENTRIES) > 30
    github = directory.get("github")
    assert github is not None and github.credential == "env_token"  # the catalog says app
    assert github.secret_label == "GitHub token"
    assert directory.get("linear") is directory.PRESETS[2]
    # the same service under its catalog name is the hand-written entry
    assert directory.get("microsoft-learn") is directory.get("learn")
    assert [e for e in directory.ENTRIES if e.endpoint.endswith("learn.microsoft.com/api/mcp")] == [
        directory.get("learn")
    ]


def test_each_way_of_signing_in_is_explained_to_the_agent() -> None:
    plans = {
        entry.credential: entry.sign_in_plan()
        for entry in directory.ENTRIES
        if entry.credential != "env_token"
    }
    assert set(plans) == {"none", "oauth", "oauth_app"}
    assert "No sign-in" in plans["none"]
    assert "provider's own page" in plans["oauth"] and "no secret is typed" in plans["oauth"]
    assert "client ID and secret on the review page (never into the chat)" in plans["oauth_app"]
    slack = directory.get("slack")
    assert slack is not None and slack.credential == "oauth_app"
    assert slack.docs and slack.docs.startswith("https://docs.slack.dev")


@pytest.mark.parametrize(
    "query, expected",
    [
        ("slack", "slack"),
        ("Sentry errors", "sentry"),
        ("project management", "asana"),
        ("monitoring", "datadog"),
        ("payments", "stripe"),
        ("figma designs", "figma"),
        ("jira", "atlassian"),
    ],
)
def test_a_service_is_found_by_name_or_by_what_it_is_for(query: str, expected: str) -> None:
    found = [entry.id for entry in directory.search(query, limit=8)]
    assert expected in found, found
    assert len(found) <= 8


def test_a_search_is_bounded_and_ignores_noise() -> None:
    assert len(directory.search("project", limit=3)) == 3
    assert directory.search("zzzzqqqq") == []
    assert directory.search("   ") == []


# --- the generator -------------------------------------------------------------------------


def _server(slug: str, sign_in: str, **overrides: Any) -> dict[str, Any]:
    server: dict[str, Any] = {
        "slug": slug,
        "name": f"{slug} MCP",
        "vendor": slug.title(),
        "operator": "first-party",
        "summary": f"  Do   things with {slug}. ",
        "categories": ["a"],
        "tags": ["a", "b"],
        "docs": f"https://docs.example.com/{slug}",
        "homepage": f"https://{slug}.example.com",
        "sign_in": sign_in,
        "endpoints": [
            {"kind": "remote", "transport": "streamable-http", "url": f"https://{slug}.example/mcp"}
        ],
    }
    server.update(overrides)
    return server


def test_the_generator_maps_how_each_service_signs_in_and_says_what_it_skipped() -> None:
    generator = _generator()
    catalog = {
        "servers": [
            _server("open-one", "Open"),
            _server("dcr-one", "OAuth (DCR)"),
            _server("both-one", "OAuth (DCR + CIMD)"),
            _server("app-one", "OAuth (own client)"),
            _server("cimd-only", "OAuth (CIMD)"),
            _server("community", "Open", operator="community"),
            _server(
                "sse-only",
                "Open",
                endpoints=[{"kind": "remote", "transport": "sse", "url": "https://x.example/sse"}],
            ),
            _server(
                "plain-http",
                "Open",
                endpoints=[
                    {
                        "kind": "remote",
                        "transport": "streamable-http",
                        "url": "http://x.example/mcp",
                    }
                ],
            ),
            _server(
                "local",
                "Local (own credentials)",
                endpoints=[{"kind": "package", "transport": "stdio"}],
            ),
        ]
    }

    rows, skipped = generator.entries_from(catalog)

    assert [(row[0], row[3]) for row in rows] == [
        ("app-one", "oauth_app"),
        ("both-one", "oauth"),
        ("dcr-one", "oauth"),
        ("open-one", "none"),
    ]
    assert rows[0][1:3] == ("App-One", "https://app-one.example/mcp")
    assert rows[0][4] == "Do things with app-one."  # whitespace tidied
    assert rows[0][5] == ("a", "b")  # topics, once each
    assert rows[0][6] == "https://docs.example.com/app-one"
    reasons = " | ".join(skipped)
    assert "cimd-only: sign-in 'OAuth (CIMD)' is not supported yet" in reasons
    assert "community: not run by the vendor (community)" in reasons
    assert "sse-only: no hosted streamable-HTTP address" in reasons
    assert "plain-http: no hosted streamable-HTTP address" in reasons
    assert "local: no hosted streamable-HTTP address" in reasons


def test_what_the_generator_writes_is_valid_python_that_reads_back_the_same() -> None:
    generator = _generator()
    rows, _ = generator.entries_from(
        {
            "servers": [
                _server("one-thing", "Open", summary='Say "hi" and é'),
                _server("two", "Open", categories=["x"], tags=[]),
            ]
        }
    )
    source = generator.render(rows, "9.9.9")
    namespace: dict[str, Any] = {}
    exec(compile(source, "directory_data.py", "exec"), namespace)  # noqa: S102

    assert namespace["ENTRIES"] == tuple(rows)
    assert "catalog tool 9.9.9, 2" in source
    assert namespace["ENTRIES"][0][4] == 'Say "hi" and é'
    assert namespace["ENTRIES"][1][5] == ("x",)  # a single topic stays a tuple
