"""
tool_registry.py
----------------
Discovers and registers executable tools (.sh, .py) from the tools directories.
Each tool file must contain a "description:" comment on any line near the top.
Multi-line descriptions are supported by continuing the comment on the next lines:

Example tool header (single-line):
    #!/bin/bash
    # description: check disk usage across all mount points

Example tool header (multi-line):
    #!/bin/bash
    # description: check disk usage across all mount points
    #   and report any volumes above 90% capacity
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Regex to match the "description:" key line in tool file header
_DESC_START_RE = re.compile(r"(?:#\s*)?description:\s*(.+)", re.IGNORECASE)
# Regex to match a continuation comment line (e.g. "#   more text") — no new key
_DESC_CONT_RE = re.compile(r"^#\s{2,}(.+)$")


@dataclass
class Tool:
    name: str           # Unique identifier derived from filename (no extension)
    path: str           # Absolute path to the script
    language: str       # "bash", "python", or "builtin" / "mcp"
    description: str    # Human-readable description extracted from file
    is_generated: bool = False  # True if created by the LLM tool creator
    is_mcp: bool = False        # True for tools provided by an MCP server
    server_name: str = ""       # MCP server name (when is_mcp=True)
    input_schema: dict = field(default_factory=dict)  # MCP JSON Schema for args


class ToolRegistry:
    """
    Scans tool directories and maintains a registry of available tools.
    Only tools present in the registry are allowed to execute (safety).
    """

    def __init__(self, tools_dirs: list[str]):
        self.tools_dirs = tools_dirs
        self._registry: dict[str, Tool] = {}
        self.refresh()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> int:
        """Rescan all directories and rebuild the registry. Returns tool count."""
        self._registry.clear()
        for directory in self.tools_dirs:
            if not os.path.isdir(directory):
                logger.debug("Tool directory not found, skipping: %s", directory)
                continue
            is_generated = "generated" in directory
            for filename in os.listdir(directory):
                if not filename.endswith((".sh", ".py")):
                    continue
                path = os.path.abspath(os.path.join(directory, filename))
                tool = self._parse_tool(path, is_generated)
                if tool:
                    if tool.name in self._registry:
                        logger.warning("Duplicate tool name '%s' — keeping first found", tool.name)
                    else:
                        self._registry[tool.name] = tool
        logger.info("Tool registry refreshed: %d tools loaded", len(self._registry))
        return len(self._registry)

    def get(self, name: str) -> Optional[Tool]:
        return self._registry.get(name)

    def all(self) -> list[Tool]:
        return list(self._registry.values())

    def exists(self, name: str) -> bool:
        return name in self._registry

    def register(self, tool: Tool) -> None:
        """Manually register a tool (used by ToolCreator after validation)."""
        self._registry[tool.name] = tool
        logger.info("Tool registered: %s (%s)", tool.name, tool.path)

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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_tool(path: str, is_generated: bool) -> Optional[Tool]:
        """Extract tool metadata from a script file."""
        try:
            with open(path, "r", errors="replace") as f:
                head = [next(f, "") for _ in range(15)]
        except Exception as exc:
            logger.warning("Could not read tool file %s: %s", path, exc)
            return None

        description = ""
        for i, line in enumerate(head):
            m = _DESC_START_RE.search(line)
            if m:
                parts = [m.group(1).strip()]
                # Collect continuation comment lines immediately following
                for cont in head[i + 1:]:
                    cm = _DESC_CONT_RE.match(cont.rstrip())
                    if cm:
                        parts.append(cm.group(1).strip())
                    else:
                        break
                description = " ".join(parts)
                break

        if not description:
            logger.debug("No description found in %s — skipping", path)
            return None

        filename = os.path.basename(path)
        name = os.path.splitext(filename)[0]
        language = "bash" if path.endswith(".sh") else "python"

        return Tool(
            name=name,
            path=path,
            language=language,
            description=description,
            is_generated=is_generated,
        )
