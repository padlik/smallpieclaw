"""
tool_registry.py — Discovers and registers tool scripts from tools/ and tools_generated/.

Each tool must contain a metadata comment:
  Bash:   # description: <one-line summary>
  Python: # description: <one-line summary>
"""

from __future__ import annotations
import os
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path

import config

logger = logging.getLogger(__name__)

DESCRIPTION_RE = re.compile(
    r"^#\s*description\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE
)


@dataclass
class Tool:
    name: str
    description: str
    path: Path
    extension: str  # ".sh" or ".py"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "path": str(self.path),
            "extension": self.extension,
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    # ── discovery ─────────────────────────────────────────────────────────────

    def scan(self) -> None:
        """Scan both tool directories and register all valid tools."""
        self._tools.clear()
        for directory in (config.TOOLS_DIR, config.TOOLS_GEN_DIR):
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                continue
            for path in sorted(directory.iterdir()):
                if path.suffix in (".sh", ".py") and path.is_file():
                    tool = self._parse_tool(path)
                    if tool:
                        self._tools[tool.name] = tool
                        logger.debug("Registered tool: %s", tool.name)
        logger.info("Tool registry: %d tools loaded.", len(self._tools))

    def _parse_tool(self, path: Path) -> Tool | None:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("Cannot read %s: %s", path, e)
            return None

        match = DESCRIPTION_RE.search(source)
        if not match:
            logger.debug("No description in %s — skipping.", path.name)
            return None

        description = match.group(1).strip()
        name = path.stem  # filename without extension
        return Tool(name=name, description=description, path=path, extension=path.suffix)

    # ── lookups ───────────────────────────────────────────────────────────────

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def is_registered(self, name: str) -> bool:
        return name in self._tools

    # ── registration of newly created tools ───────────────────────────────────

    def register_file(self, path: Path) -> Tool | None:
        """Parse and register a single file, replacing any existing entry."""
        tool = self._parse_tool(path)
        if tool:
            self._tools[tool.name] = tool
            logger.info("Registered new tool: %s", tool.name)
        return tool

    def summary(self) -> str:
        """Human-readable list of all tools."""
        lines = []
        for t in self._tools.values():
            lines.append(f"• {t.name}: {t.description}")
        return "\n".join(lines) if lines else "No tools registered."
