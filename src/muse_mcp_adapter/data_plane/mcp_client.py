from collections.abc import Mapping
from typing import Protocol

from ..domain.models import McpPromptResult, McpResourceResult, ResolvedCredential


class McpReadClient(Protocol):
    def read_resource(
        self,
        *,
        endpoint: str,
        uri: str,
        credential: ResolvedCredential,
    ) -> McpResourceResult: ...

    def get_prompt(
        self,
        *,
        endpoint: str,
        name: str,
        arguments: Mapping[str, str],
        credential: ResolvedCredential,
    ) -> McpPromptResult: ...
