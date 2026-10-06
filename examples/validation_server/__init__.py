"""Atlas: a configurable MCP server for validating the Muse MCP Adapter.

Atlas is a small issue tracker and knowledge base. It exists to exercise every part of MCP the
adapter has to carry, and it keeps a ledger of real side effects so a test can prove whether a
write happened once, twice, or not at all. See docs/validation-server.md.
"""

from .app import AtlasOptions, build_app
from .state import AtlasState

__all__ = ["AtlasOptions", "AtlasState", "build_app"]
