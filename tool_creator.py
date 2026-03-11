"""
tool_creator.py — Allows the LLM to create new tool scripts at runtime.

Safety checks:
  - Forbidden patterns (rm -rf, etc.)
  - Max file size
  - Optional LLM safety review
  - Saves to tools_generated/ only
"""

from __future__ import annotations
import logging
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

import config
import llm_client
from tool_registry import Tool, ToolRegistry
from tool_index import ToolIndex

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── forbidden patterns ────────────────────────────────────────────────────────
FORBIDDEN_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"rm\s+-[rRfF]*\s+/",          # rm -rf /
        r":\s*\(\s*\)\s*\{.*\}\s*;",   # fork bomb
        r"dd\s+if=",                    # dd overwrite
        r"mkfs\.",                      # format disk
        r">\s*/dev/sd",                 # overwrite block device
        r"chmod\s+777\s+/",            # mass permission change
        r"curl.*\|\s*(ba)?sh",          # curl-pipe-shell
        r"wget.*\|\s*(ba)?sh",
        r"nc\s+-[le]",                  # netcat listener
        r"eval\s*\$\(",                 # eval injection
        r"base64\s+-d.*\|.*sh",        # encoded payload
    ]
]

SUPPORTED_LANGUAGES = {"bash": ".sh", "python": ".py"}


class ToolCreationError(Exception):
    pass


class ToolCreator:
    def __init__(self, registry: ToolRegistry, index: ToolIndex):
        self._registry = registry
        self._index = index

    def create(self, name: str, language: str, code: str, llm_review: bool = True) -> Tool:
        """
        Validate, save, register, and index a new tool.
        Returns the created Tool object.
        """
        language = language.lower().strip()
        if language not in SUPPORTED_LANGUAGES:
            raise ToolCreationError(
                f"Unsupported language '{language}'. Supported: {list(SUPPORTED_LANGUAGES)}"
            )

        # Sanitise tool name
        name = re.sub(r"[^\w]", "_", name.strip().lower())
        if not name:
            raise ToolCreationError("Tool name is empty.")

        self._check_forbidden(code)
        self._check_size(code)

        if llm_review:
            self._llm_safety_review(name, code)

        ext = SUPPORTED_LANGUAGES[language]
        path = config.TOOLS_GEN_DIR / f"{name}{ext}"
        config.TOOLS_GEN_DIR.mkdir(parents=True, exist_ok=True)

        path.write_text(code, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        logger.info("Created tool file: %s", path)

        tool = self._registry.register_file(path)
        if tool is None:
            path.unlink(missing_ok=True)
            raise ToolCreationError(
                f"Tool '{name}' has no valid '# description:' comment — cannot register."
            )

        self._index.add_tool(tool)
        return tool

    # ── validation ─────────────────────────────────────────────────────────────

    def _check_forbidden(self, code: str) -> None:
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(code):
                raise ToolCreationError(
                    f"Forbidden pattern detected in generated code: {pattern.pattern!r}"
                )

    def _check_size(self, code: str) -> None:
        size = len(code.encode("utf-8"))
        if size > config.MAX_TOOL_FILE_SIZE:
            raise ToolCreationError(
                f"Generated code size {size}B exceeds limit {config.MAX_TOOL_FILE_SIZE}B."
            )

    def _llm_safety_review(self, name: str, code: str) -> None:
        """Ask the LLM to check if the code is safe. Raises on refusal."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a security reviewer for a Raspberry Pi automation system. "
                    "Review the provided script for dangerous operations. "
                    "Respond ONLY with JSON: "
                    '{"safe": true/false, "reason": "brief explanation"}'
                ),
            },
            {
                "role": "user",
                "content": f"Tool name: {name}\n\nCode:\n```\n{code}\n```",
            },
        ]
        try:
            result = llm_client.chat_json(messages)
            if not result.get("safe", False):
                raise ToolCreationError(
                    f"LLM safety review rejected tool '{name}': {result.get('reason', 'unknown')}"
                )
            logger.info("LLM safety review passed for tool '%s'.", name)
        except ToolCreationError:
            raise
        except Exception as e:
            logger.warning("LLM safety review failed (%s) — proceeding with local checks only.", e)
