from pathlib import Path

from muse_mcp_adapter.credentials.resolver import InMemoryCredentialResolver
from muse_mcp_adapter.data_plane.read_service import ReadDataPlaneService
from tests.fixtures.active_runtime import active_repository
from tests.fixtures.read_runtime import ConformanceReadClient, FakeMuseClient


def test_muse_reads_resources_templates_and_prompts_through_active_catalog(
    tmp_path: Path,
) -> None:
    with active_repository(tmp_path / "adapter.db") as repository:
        credentials = InMemoryCredentialResolver()
        credentials.register(
            local_identity_id="muse-user-1",
            connection_id="project-connection-1",
            server_id="project-system",
            credential_ref="secret://project-system/muse-user-1",
            access_token="downstream-token",
            scopes=frozenset({"issues:read", "workspace:read"}),
        )
        mcp = ConformanceReadClient()
        muse = FakeMuseClient(
            ReadDataPlaneService(repository, credentials, mcp),
            server_id="project-system",
            local_identity_id="muse-user-1",
            connection_id="project-connection-1",
        )

        guide = muse.execute("read_workspace_guide", {})
        issue = muse.execute("read_issue_document", {"issueId": "ISSUE-42"})
        prompt = muse.execute("get_triage_prompt", {"issueId": "ISSUE-42"})

        assert guide.primitive_type == "resource"
        assert guide.contents[0].mime_type == "text/markdown"
        assert issue.primitive_type == "resource_template"
        assert issue.contents[0].uri == "mcp://issues/ISSUE-42/document"
        assert prompt.primitive_type == "prompt"
        assert prompt.messages[0].role == "user"
        assert prompt.provenance["catalogVersion"] == "1.0.0"
        assert all(
            call.credential_ref == "secret://project-system/muse-user-1"
            for call in [*mcp.resource_calls, *mcp.prompt_calls]
        )
        assert "downstream-token" not in repr((guide, issue, prompt))
