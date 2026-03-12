"""
tool_creator.py
---------------
Handles dynamic tool creation requested by the LLM agent.
Validates generated code for dangerous patterns before writing to disk.
"""

import logging
import os
import re
import stat

from tool_index import ToolIndex
from tool_registry import Tool, ToolRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dangerous pattern blocklist
# ---------------------------------------------------------------------------
# These patterns are checked against generated code. Any match causes rejection.
_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    # Destructive filesystem
    (r"\brm\s+-rf\s+/", "destructive rm -rf /"),
    (r"\bdd\b.*of=/dev/", "raw device write with dd"),
    (r":>\s*/dev/", "truncate device node"),
    # Fork bombs
    (r":\(\)\{.*:\|:&\}", "fork bomb"),
    # Privilege escalation
    (r"\bsudo\b", "sudo usage"),
    (r"\bsu\b\s+-", "su - usage"),
    (r"\bchmod\s+[0-7]*777", "world-writable chmod"),
    # Network exfiltration
    (r"\bcurl\b.*\|\s*bash", "curl pipe to bash"),
    (r"\bwget\b.*\|\s*bash", "wget pipe to bash"),
    # Python-specific
    (r"\bos\.system\s*\(", "os.system call"),
    (r"\beval\s*\(", "eval call"),
    (r"\bexec\s*\(", "exec call"),
    (r"__import__\s*\(", "__import__ usage"),
    # Reverse shells
    (r"/dev/tcp/", "/dev/tcp reverse shell"),
    (r"\bnc\b.*-e\b", "netcat -e reverse shell"),
]

_COMPILED: list[tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE | re.DOTALL), label)
    for pat, label in _DANGEROUS_PATTERNS
]


def _validate_code(code: str) -> tuple[bool, str]:
    """
    Check generated code against the blocklist.
    Returns (is_safe, reason_if_not_safe).
    """
    for pattern, label in _COMPILED:
        if pattern.search(code):
            return False, f"Blocked pattern detected: {label}"
    return True, ""


def _sanitize_name(name: str) -> str:
    """Ensure the tool name is a safe filename (alphanumeric + underscores)."""
    clean = re.sub(r"[^\w]", "_", name.lower())
    return clean[:64]  # Limit length


class ToolCreator:
    """
    Validates and stores LLM-generated tools.
    Registers them in the ToolRegistry and indexes them semantically.
    """

    def __init__(
        self,
        generated_dir: str,
        registry: ToolRegistry,
        index: ToolIndex,
    ):
        self.generated_dir = generated_dir
        self.registry = registry
        self.index = index
        os.makedirs(generated_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, name: str, language: str, code: str, description: str = "") -> dict:
        """
        Validate and persist a new tool.

        Returns:
          {"success": True, "name": ..., "path": ...}   on success
          {"success": False, "error": ...}               on failure
        """
        # 1. Sanitise inputs
        safe_name = _sanitize_name(name)
        if not safe_name:
            return {"success": False, "error": "Invalid tool name."}

        language = language.lower().strip()
        if language not in ("bash", "python", "sh"):
            return {"success": False, "error": f"Unsupported language: {language}"}
        if language == "sh":
            language = "bash"

        # 2. Validate code safety
        ok, reason = _validate_code(code)
        if not ok:
            logger.warning("Tool creation blocked for '%s': %s", safe_name, reason)
            return {"success": False, "error": f"Code validation failed: {reason}"}

        # 3. Ensure description comment is in the code
        if not description:
            description = f"auto-generated tool: {safe_name}"
        code_with_desc = self._inject_description(code, language, description, safe_name)

        # 4. Write file
        ext = ".sh" if language == "bash" else ".py"
        filename = safe_name + ext
        path = os.path.abspath(os.path.join(self.generated_dir, filename))

        # Guard: ensure path stays inside generated_dir (no traversal)
        allowed = os.path.abspath(self.generated_dir)
        if not path.startswith(allowed + os.sep):
            return {"success": False, "error": "Path traversal detected in tool name."}

        try:
            with open(path, "w") as f:
                f.write(code_with_desc)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP)
        except Exception as exc:
            return {"success": False, "error": f"Could not write tool file: {exc}"}

        # 5. Register and index
        tool = Tool(
            name=safe_name,
            path=path,
            language=language,
            description=description,
            is_generated=True,
        )
        self.registry.register(tool)
        self.index.add_tool(tool)

        logger.info("New tool created: %s (%s)", safe_name, path)
        return {"success": True, "name": safe_name, "path": path}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _inject_description(code: str, language: str, description: str, name: str) -> str:
        """Prepend a proper shebang + description comment if not already present."""
        if "description:" in code.lower():
            return code  # Already has a description

        if language == "bash":
            header = f"#!/bin/bash\n# description: {description}\n# generated tool: {name}\n\n"
        else:
            header = f"#!/usr/bin/env python3\n# description: {description}\n# generated tool: {name}\n\n"

        return header + code
