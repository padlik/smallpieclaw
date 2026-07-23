"""
tool_registry.py
----------------
MCP tool registry — registers and looks up tools provided by MCP servers.

The registry no longer scans local script directories. Tools are added only
via register_mcp_tools() from an MCP client and removed via
unregister_mcp_server(). All lookups share the same Tool dataclass used by
the previous file-scanning implementation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str           # Unique tool identifier (MCP tool name)
    language: str       # "mcp" for MCP-provided tools
    description: str    # Human-readable description
    is_mcp: bool = False        # True for tools provided by an MCP server
    server_name: str = ""       # MCP server name (when is_mcp=True)
    input_schema: dict = field(default_factory=dict)  # MCP JSON Schema for args


class ToolRegistry:
    """
    Maintains a registry of tools provided by MCP servers.
    Only tools present in the registry are allowed to execute (safety).
    """

    def __init__(self):
        self._registry: dict[str, Tool] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[Tool]:
        return self._registry.get(name)

    def all(self) -> list[Tool]:
        return list(self._registry.values())

    def exists(self, name: str) -> bool:
        return name in self._registry

    def register_mcp_tools(self, server_name: str, tools: list[Tool]) -> None:
        """Register MCP tools from the given server (replaces any prior tools for that server)."""
        self.unregister_mcp_server(server_name)
        for tool in tools:
            if tool.name in self._registry:
                logger.warning("MCP tool '%s' from server '%s' conflicts with existing tool — skipping",
                               tool.name, server_name)
            else:
                self._registry[tool.name] = tool
        logger.info("MCP server '%s': registered %d tool(s)", server_name, len(tools))

    def unregister_mcp_server(self, server_name: str) -> int:
        """Remove all tools belonging to the given MCP server. Returns count removed."""
        to_remove = [n for n, t in self._registry.items() if t.is_mcp and t.server_name == server_name]
        for name in to_remove:
            del self._registry[name]
        if to_remove:
            logger.info("MCP server '%s': unregistered %d tool(s)", server_name, len(to_remove))
        return len(to_remove)

    def summary(self) -> str:
        """Return a compact multi-line summary of all registered tools."""
        if not self._registry:
            return "No tools registered."
        lines = [f"  {t.name}: {t.description}" for t in self._registry.values()]
        return "\n".join(lines)
