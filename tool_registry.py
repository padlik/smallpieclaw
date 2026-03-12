"""
tool_registry.py
----------------
Discovers and registers executable tools (.sh, .py) from the tools directories.
Each tool file must contain a "description:" comment on any line near the top.

Example tool header:
    #!/bin/bash
    # description: check disk usage across all mount points
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Regex to extract description from tool file header (first 10 lines)
_DESC_RE = re.compile(r"(?:#\s*)?description:\s*(.+)", re.IGNORECASE)


@dataclass
class Tool:
    name: str           # Unique identifier derived from filename (no extension)
    path: str           # Absolute path to the script
    language: str       # "bash" or "python"
    description: str    # Human-readable description extracted from file
    is_generated: bool = False  # True if created by the LLM tool creator


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
                head = [next(f, "") for _ in range(10)]
        except Exception as exc:
            logger.warning("Could not read tool file %s: %s", path, exc)
            return None

        description = ""
        for line in head:
            m = _DESC_RE.search(line)
            if m:
                description = m.group(1).strip()
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
